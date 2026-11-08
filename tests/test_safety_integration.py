"""
Tests for Vibe-Trading inspired safety infrastructure.
"""

import json
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, PropertyMock

import pytest

from safety import (
    MandateGate,
    MandateViolationType,
    MandateContext,
    KillSwitch,
    KillSwitchState,
    AuditLedger,
    AuditEntry,
    AuditEntryType,
    PreTradeAdvisoryInterface,
    AdvisoryVerdict,
    AdvisoryContext,
    AdvisoryResult,
    AdvisoryOrchestrator,
    AggregatedVerdict,
    MockAdvisory,
    PaperLiveBoundary,
    AccountMode,
    ModeMismatchError,
    SizingHook,
    ConnectorPaperLiveBoundary,
    HTTPAdvisoryProvider,
    HTTPAdvisoryConfig,
)


class TestMandateGate:
    """Tests for MandateGate structural compliance checks."""

    def test_allows_valid_order(self):
        """Valid order passes mandate gate."""
        gate = MandateGate()
        context = MandateContext(
            symbol="AAPL",
            side="buy",
            notional_usd=10000.0,
            qty=50,
            account_equity=100000.0,
            account_cash=50000.0,
            account_buying_power=100000.0,
            account_mode="paper",
            open_positions={},
            open_position_count=0,
        )
        violations = gate.check(context)
        assert len(violations) == 0

    def test_blocks_kill_switch_active(self):
        """Kill switch active blocks all orders."""
        gate = MandateGate()
        context = MandateContext(
            symbol="AAPL",
            side="buy",
            notional_usd=10000.0,
            qty=50,
            account_equity=100000.0,
            account_cash=50000.0,
            account_buying_power=100000.0,
            account_mode="paper",
            open_positions={},
            open_position_count=0,
            kill_switch_active=True,
        )
        violations = gate.check(context)
        assert len(violations) == 1
        assert violations[0].violation_type == MandateViolationType.KILL_SWITCH_ACTIVE

    def test_blocks_symbol_not_allowed(self):
        """Symbol not in allowed universe is blocked."""
        gate = MandateGate()
        context = MandateContext(
            symbol="INVALID",
            side="buy",
            notional_usd=10000.0,
            qty=50,
            account_equity=100000.0,
            account_cash=50000.0,
            account_buying_power=100000.0,
            account_mode="paper",
            open_positions={},
            open_position_count=0,
        )
        violations = gate.check(context)
        assert any(v.violation_type == MandateViolationType.SYMBOL_NOT_ALLOWED for v in violations)

    def test_blocks_per_order_notional_cap(self):
        """Order exceeding per-order notional cap is blocked."""
        gate = MandateGate()
        context = MandateContext(
            symbol="AAPL",
            side="buy",
            notional_usd=100000.0,  # Exceeds default 50000
            qty=500,
            account_equity=1000000.0,
            account_cash=500000.0,
            account_buying_power=1000000.0,
            account_mode="paper",
            open_positions={},
            open_position_count=0,
        )
        violations = gate.check(context)
        assert any(v.violation_type == MandateViolationType.NOTIONAL_EXCEEDS_PER_ORDER_CAP for v in violations)

    def test_blocks_leverage_limit(self):
        """Order exceeding leverage limit is blocked."""
        gate = MandateGate()
        context = MandateContext(
            symbol="AAPL",
            side="buy",
            notional_usd=200000.0,  # 2x leverage on 100k equity
            qty=1000,
            account_equity=100000.0,
            account_cash=50000.0,
            account_buying_power=100000.0,
            account_mode="paper",
            open_positions={},
            open_position_count=0,
        )
        violations = gate.check(context)
        assert any(v.violation_type == MandateViolationType.LEVERAGE_EXCEEDS_LIMIT for v in violations)

    def test_blocks_position_concentration(self):
        """Order exceeding position concentration limit is blocked."""
        gate = MandateGate()
        open_positions = {
            "AAPL": {"qty": 100, "current_price": 150.0},  # $15k existing
        }
        context = MandateContext(
            symbol="AAPL",
            side="buy",
            notional_usd=20000.0,  # Would make $35k = 35% of 100k
            qty=100,
            account_equity=100000.0,
            account_cash=50000.0,
            account_buying_power=100000.0,
            account_mode="paper",
            open_positions=open_positions,
            open_position_count=1,
        )
        violations = gate.check(context)
        assert any(v.violation_type == MandateViolationType.POSITION_CONCENTRATION_EXCEEDED for v in violations)

    def test_daily_counters_restore_from_audit_entry_fields_after_restart(self, tmp_path, monkeypatch):
        from safety.audit_ledger import AuditEntry, AuditEntryType

        class Ledger:
            def get_entries_for_date(self, date):
                if date != "2099-01-02":
                    return []
                return [
                    AuditEntry(
                        entry_id="1", entry_type=AuditEntryType.ORDER_SUBMITTED,
                        timestamp="2099-01-02T15:00:00+00:00",
                        previous_hash="", entry_hash="", symbol="AAPL", side="buy",
                        notional_usd=12000.0, qty=80, price=150.0,
                    ),
                    AuditEntry(
                        entry_id="2", entry_type=AuditEntryType.ORDER_SUBMITTED,
                        timestamp="2099-01-02T16:00:00+00:00",
                        previous_hash="", entry_hash="", symbol="MSFT", side="buy",
                        notional_usd=8000.0, qty=20, price=400.0,
                    ),
                ]

        monkeypatch.setattr(
            "safety.mandate_gate.datetime",
            type("FixedDateTime", (), {
                "now": staticmethod(lambda tz=None: datetime(2099, 1, 2, 17, 0, tzinfo=timezone.utc)),
                "combine": staticmethod(datetime.combine),
                "min": datetime.min,
                "fromisoformat": staticmethod(datetime.fromisoformat),
            }),
        )
        gate = MandateGate(audit_ledger=Ledger())

        assert gate.get_daily_summary()["daily_notional_usd"] == 20000.0
        assert gate.get_daily_summary()["daily_order_count"] == 2
        assert gate.get_daily_summary()["daily_trade_count"] == 2
        assert gate._get_symbol_daily_notional("AAPL") == 12000.0

    def test_blocks_daily_notional_cap(self):
        """Order exceeding daily notional cap is blocked."""
        gate = MandateGate()
        gate._daily_notional_usd = 480000.0  # Near daily cap of 500k
        gate._last_reset_date = datetime.now(timezone.utc).date().isoformat()
        context = MandateContext(
            symbol="AAPL",
            side="buy",
            notional_usd=30000.0,  # Would exceed 500k
            qty=150,
            account_equity=1000000.0,
            account_cash=500000.0,
            account_buying_power=1000000.0,
            account_mode="paper",
            open_positions={},
            open_position_count=0,
        )
        violations = gate.check(context)
        assert any(v.violation_type == MandateViolationType.NOTIONAL_EXCEEDS_DAILY_CAP for v in violations)


class TestKillSwitch:
    """Tests for KillSwitch emergency halt mechanism."""

    def test_initial_state_inactive(self):
        """Kill switch starts inactive."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sentinel = Path(tmpdir) / "kill_switch.sentinel"
            state_file = Path(tmpdir) / "kill_switch_state.json"
            ks = KillSwitch(sentinel_path=sentinel, state_path=state_file)
            assert ks.get_state() == KillSwitchState.INACTIVE
            assert ks.allows_new_entries() is True
            assert ks.allows_cancellations() is True

    def test_activate_blocks_entries(self):
        """Activating kill switch blocks new entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sentinel = Path(tmpdir) / "kill_switch.sentinel"
            state_file = Path(tmpdir) / "kill_switch_state.json"
            ks = KillSwitch(sentinel_path=sentinel, state_path=state_file)

            ks.activate("Test activation")
            assert ks.get_state() == KillSwitchState.ACTIVE
            assert ks.allows_new_entries() is False
            assert ks.allows_cancellations() is True  # Cancellations always allowed

    def test_deactivate_allows_entries(self):
        """Deactivating kill switch allows new entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sentinel = Path(tmpdir) / "kill_switch.sentinel"
            state_file = Path(tmpdir) / "kill_switch_state.json"
            ks = KillSwitch(sentinel_path=sentinel, state_path=state_file)

            ks.activate("Test")
            ks.deactivate("Test deactivation")
            assert ks.get_state() == KillSwitchState.INACTIVE
            assert ks.allows_new_entries() is True

    def test_persists_across_restart(self):
        """Kill switch state persists across restart."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sentinel = Path(tmpdir) / "kill_switch.sentinel"
            state_file = Path(tmpdir) / "kill_switch_state.json"

            # First instance - activate
            ks1 = KillSwitch(sentinel_path=sentinel, state_path=state_file)
            ks1.activate("Persist test")

            # Second instance - should load active state
            ks2 = KillSwitch(sentinel_path=sentinel, state_path=state_file)
            assert ks2.get_state() == KillSwitchState.ACTIVE
            assert ks2.allows_new_entries() is False

    def test_flatten_latch_prevents_double_flatten(self):
        """Flatten latch prevents double flatten on restart."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sentinel = Path(tmpdir) / "kill_switch.sentinel"
            state_file = Path(tmpdir) / "kill_switch_state.json"

            ks1 = KillSwitch(sentinel_path=sentinel, state_path=state_file)
            ks1.activate("Test")
            ks1.start_flatten("Flatten test")

            # Restart - should have flatten_latch = True
            ks2 = KillSwitch(sentinel_path=sentinel, state_path=state_file)
            assert ks2._flatten_latch is True
            # Second flatten should fail
            result = ks2.start_flatten("Second flatten")
            assert result is False


class TestAuditLedger:
    """Tests for AuditLedger hash-chained immutable log."""

    def test_log_and_read_entry(self):
        """Can log and read entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = AuditLedger(ledger_dir=Path(tmpdir))

            ledger.log_order_submitted(
                symbol="AAPL",
                side="buy",
                notional_usd=10000.0,
                qty=50,
                price=200.0,
                order_id="ORD-123",
                context={"strategy": "test"},
            )

            today = datetime.now(timezone.utc).date().isoformat()
            entries = ledger.get_entries_for_date(today)
            assert len(entries) == 1
            assert entries[0].symbol == "AAPL"
            assert entries[0].entry_type == AuditEntryType.ORDER_SUBMITTED

    def test_hash_chain_integrity(self):
        """Hash chain detects tampering."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = AuditLedger(ledger_dir=Path(tmpdir))

            ledger.log_order_submitted("AAPL", "buy", 10000, 50, 200, "ORD-1", {})
            ledger.log_order_submitted("MSFT", "buy", 5000, 25, 200, "ORD-2", {})

            # Verify chain
            result = ledger.verify_chain()
            assert result['valid'] is True
            assert result['entries_checked'] == 2

    def test_tampering_detected(self):
        """Modifying a file breaks hash chain."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = AuditLedger(ledger_dir=Path(tmpdir))

            ledger.log_order_submitted("AAPL", "buy", 10000, 50, 200, "ORD-1", {})
            ledger.log_order_submitted("MSFT", "buy", 5000, 25, 200, "ORD-2", {})

            # Tamper with first entry
            today = datetime.now(timezone.utc).date().isoformat()
            file_path = Path(tmpdir) / f"ledger_{today}.jsonl"
            with open(file_path, 'r') as f:
                lines = f.readlines()
            # Corrupt first line
            lines[0] = lines[0].replace('AAPL', 'TAMPERED')
            with open(file_path, 'w') as f:
                f.writelines(lines)

            # Verify should fail
            result = ledger.verify_chain()
            assert result['valid'] is False


class TestMockAdvisory:
    """Tests for MockAdvisory test provider."""

    def test_returns_configured_verdict(self):
        """Mock returns configured verdict."""
        mock = MockAdvisory(verdict=AdvisoryVerdict.REJECT, concerns=["Too risky"])
        context = AdvisoryContext(
            symbol="AAPL", side="buy", notional_usd=10000, qty=50,
            account_equity=100000, account_drawdown=0.0, open_position_count=0,
            total_exposure_usd=0.0, funding_usd=50000,
        )
        result = mock.review(context)
        assert result.verdict == AdvisoryVerdict.REJECT
        assert result.concerns == ["Too risky"]

    def test_records_call_history(self):
        """Mock records call history for assertions."""
        mock = MockAdvisory()
        context = AdvisoryContext(
            symbol="AAPL", side="buy", notional_usd=10000, qty=50,
            account_equity=100000, account_drawdown=0.0, open_position_count=0,
            total_exposure_usd=0.0, funding_usd=50000,
        )
        mock.review(context)
        mock.review(context)
        history = mock.get_call_history()
        assert len(history) == 2
        assert history[0].symbol == "AAPL"

    def test_exception_injection(self):
        """Mock can inject exceptions for fail-open testing."""
        mock = MockAdvisory(raise_exception=True, exception_message="Service down")
        context = AdvisoryContext(
            symbol="AAPL", side="buy", notional_usd=10000, qty=50,
            account_equity=100000, account_drawdown=0.0, open_position_count=0,
            total_exposure_usd=0.0, funding_usd=50000,
        )
        with pytest.raises(Exception, match="Service down"):
            mock.review(context)


class TestAdvisoryOrchestrator:
    """Tests for AdvisoryOrchestrator aggregation logic."""

    def test_approve_all_approve(self):
        """All approve -> final approve."""
        mock1 = MockAdvisory(provider_id="p1", verdict=AdvisoryVerdict.APPROVE)
        mock2 = MockAdvisory(provider_id="p2", verdict=AdvisoryVerdict.APPROVE)
        orch = AdvisoryOrchestrator(providers=[mock1, mock2])

        context = AdvisoryContext(
            symbol="AAPL", side="buy", notional_usd=10000, qty=50,
            account_equity=100000, account_drawdown=0.0, open_position_count=0,
            total_exposure_usd=0.0, funding_usd=50000,
        )
        result = orch.review(context)
        assert result.final_verdict == AdvisoryVerdict.APPROVE

    def test_reject_any_reject(self):
        """Any reject -> final reject."""
        mock1 = MockAdvisory(provider_id="p1", verdict=AdvisoryVerdict.APPROVE)
        mock2 = MockAdvisory(provider_id="p2", verdict=AdvisoryVerdict.REJECT)
        orch = AdvisoryOrchestrator(providers=[mock1, mock2])

        context = AdvisoryContext(
            symbol="AAPL", side="buy", notional_usd=10000, qty=50,
            account_equity=100000, account_drawdown=0.0, open_position_count=0,
            total_exposure_usd=0.0, funding_usd=50000,
        )
        result = orch.review(context)
        assert result.final_verdict == AdvisoryVerdict.REJECT

    def test_concerns_any_concerns(self):
        """Any concerns -> final approve_with_concerns."""
        mock1 = MockAdvisory(provider_id="p1", verdict=AdvisoryVerdict.APPROVE)
        mock2 = MockAdvisory(provider_id="p2", verdict=AdvisoryVerdict.APPROVE_WITH_CONCERNS, concerns=["High volatility"])
        orch = AdvisoryOrchestrator(providers=[mock1, mock2])

        context = AdvisoryContext(
            symbol="AAPL", side="buy", notional_usd=10000, qty=50,
            account_equity=100000, account_drawdown=0.0, open_position_count=0,
            total_exposure_usd=0.0, funding_usd=50000,
        )
        result = orch.review(context)
        assert result.final_verdict == AdvisoryVerdict.APPROVE_WITH_CONCERNS
        assert "High volatility" in result.all_concerns

    def test_fail_open_on_timeout(self):
        """Provider timeout -> fail open (REVIEW_UNAVAILABLE)."""
        mock = MockAdvisory(provider_id="slow", delay_ms=500)  # 500ms delay
        orch = AdvisoryOrchestrator(providers=[mock], timeout_ms=100)  # 100ms timeout

        context = AdvisoryContext(
            symbol="AAPL", side="buy", notional_usd=10000, qty=50,
            account_equity=100000, account_drawdown=0.0, open_position_count=0,
            total_exposure_usd=0.0, funding_usd=50000,
        )
        result = orch.review(context)
        assert result.final_verdict == AdvisoryVerdict.REVIEW_UNAVAILABLE


class TestPaperLiveBoundary:
    """Tests for PaperLiveBoundary structural mode separation."""

    def test_verify_mode_paper(self):
        """Verifies paper mode correctly."""
        mock_client = Mock()
        mock_client._paper = True
        mock_client.get_account.return_value = Mock(paper=True, status="ACTIVE")

        boundary = PaperLiveBoundary(
            expected_mode=AccountMode.PAPER,
            broker_client=mock_client,
        )
        result = boundary.verify_mode()
        assert result.verified_mode == AccountMode.PAPER
        assert result.matched is True

    def test_verify_mode_live(self):
        """Verifies live mode correctly."""
        mock_client = Mock()
        mock_client._paper = False
        mock_client.get_account.return_value = Mock(paper=False, status="ACTIVE")

        boundary = PaperLiveBoundary(
            expected_mode=AccountMode.LIVE,
            broker_client=mock_client,
        )
        result = boundary.verify_mode()
        assert result.verified_mode == AccountMode.LIVE
        assert result.matched is True

    def test_mismatch_raises_error(self):
        """Mode mismatch raises ModeMismatchError."""
        mock_client = Mock()
        mock_client.get_account.return_value = Mock(paper=True, status="ACTIVE")

        boundary = PaperLiveBoundary(
            expected_mode=AccountMode.LIVE,  # Expect live, get paper
            broker_client=mock_client,
        )
        with pytest.raises(ModeMismatchError):
            boundary.enforce_mode("test_operation")


class TestConnectorPaperLiveBoundary:
    """Tests for ConnectorPaperLiveBoundary volume/notional checks."""

    def _make_boundary(self, **kwargs):
        """Create a boundary with properly mocked client."""
        mock_client = Mock()
        # Configure the mock to return False for _paper attribute
        type(mock_client)._paper = PropertyMock(return_value=False)

        # Create a proper mock account that get_account() returns
        mock_account = Mock()
        type(mock_account).status = PropertyMock(return_value="ACTIVE")
        type(mock_account).paper = PropertyMock(return_value=False)
        mock_client.get_account.return_value = mock_account

        mock_client.get_latest_price.return_value = 200.0
        return ConnectorPaperLiveBoundary(
            connector_name="test",
            expected_mode=AccountMode.LIVE,
            broker_client=mock_client,
            **kwargs
        )

    def test_allows_within_limits(self):
        """Allows order within volume and notional limits."""
        boundary = self._make_boundary(
            max_order_volume=1000,
            max_order_notional_usd=50000,
        )
        result = boundary.check_order("AAPL", "buy", 100, price=200.0)
        assert result['allowed'] is True
        assert result['notional_usd'] == 20000.0

    def test_blocks_volume_exceeded(self):
        """Blocks order exceeding max volume."""
        boundary = self._make_boundary(
            max_order_volume=100,
            max_order_notional_usd=50000,
        )
        result = boundary.check_order("AAPL", "buy", 200, price=200.0)
        assert result['allowed'] is False
        assert "volume" in result['reason'].lower()

    def test_blocks_notional_exceeded(self):
        """Blocks order exceeding max notional USD."""
        boundary = self._make_boundary(
            max_order_volume=1000,
            max_order_notional_usd=10000,  # $10k cap
        )
        result = boundary.check_order("AAPL", "buy", 100, price=200.0)  # $20k notional
        assert result['allowed'] is False
        assert "notional" in result['reason'].lower()

    def test_fail_closed_no_price(self):
        """Fail-closed when price unavailable for notional check."""
        boundary = self._make_boundary(
            max_order_volume=1000,
            max_order_notional_usd=50000,
        )
        # Override price to None
        boundary._sizing_hook.boundary.broker_client.get_latest_price.return_value = None
        result = boundary.check_order("AAPL", "buy", 100, price=None)
        assert result['allowed'] is False
        assert result.get('fail_closed') is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])