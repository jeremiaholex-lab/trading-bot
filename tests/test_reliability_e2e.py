"""End-to-end reliability tests (founder board condition 5).

The board asked to exercise the complete data-to-order-to-alert path for stale
data, rejected/partial orders, outages, recovery, and reconciliation against the
actual broker/account state. Much of this is already covered elsewhere
(test_alpaca_broker, test_data_quality_integration, test_protective_orders,
test_advanced_risk_gates); this file pins the gaps:

- Fatal auth must NOT be retried (the board's first safety invariant: invalid
  credentials are fatal, not silently retried or simulated).
- The broker's live positions are the source of truth the bot reconciles to.
- What happens on a broker outage (characterization, so a regression is caught).
"""

import types

import pytest

from execution.alpaca_broker import (
    AlpacaBroker,
    OrderSubmissionUncertain,
    retry_api_call,
)


# --- Fatal auth is not retried ---------------------------------------------

def test_retry_api_call_does_not_retry_fatal_auth():
    """Invalid credentials raise immediately, with no retry and no sleep."""
    calls = 0
    def unauthorized():
        nonlocal calls
        calls += 1
        raise ValueError("Unauthorized: invalid API key")

    with pytest.raises(ValueError, match="Unauthorized"):
        retry_api_call(unauthorized, max_retries=3, base_delay=0.01)
    assert calls == 1  # never retried: fatal


def test_order_submission_reconciles_ambiguous_response_without_resubmitting():
    broker = AlpacaBroker(force_simulator=True)

    class Client:
        submitted = 0

        def submit_order(self, request):
            self.submitted += 1
            raise TimeoutError("response timed out")

        def get_order_by_client_id(self, client_order_id):
            assert client_order_id == "bot-test-id"
            return types.SimpleNamespace(id="accepted-order")

    client = Client()
    broker.client = client
    request = types.SimpleNamespace(client_order_id="bot-test-id")

    order = broker._submit_order_once(request, "test order")

    assert order.id == "accepted-order"
    assert client.submitted == 1


def test_uncertain_order_submission_latches_kill_switch_without_retry():
    broker = AlpacaBroker(force_simulator=True)

    class Client:
        submitted = 0

        def submit_order(self, request):
            self.submitted += 1
            raise TimeoutError("response timed out")

        def get_order_by_client_id(self, client_order_id):
            raise ConnectionError("broker still unavailable")

    class Switch:
        def __init__(self):
            self.reasons = []

        def activate(self, reason, triggered_by):
            self.reasons.append((reason, triggered_by))

    client = Client()
    broker.client = client
    broker.kill_switch = Switch()
    request = types.SimpleNamespace(client_order_id="bot-test-id")

    with pytest.raises(OrderSubmissionUncertain, match="no retry was sent"):
        broker._submit_order_once(request, "test order")

    assert client.submitted == 1
    assert broker.kill_switch.reasons


def test_retry_api_call_does_not_retry_forbidden():
    calls = 0
    def forbidden():
        nonlocal calls
        calls += 1
        raise PermissionError("Forbidden: 403")

    with pytest.raises(PermissionError, match="Forbidden"):
        retry_api_call(forbidden, max_retries=3, base_delay=0.01)
    assert calls == 1


# --- Position reconciliation -------------------------------------------------

class _FakeClient:
    """Minimal fake Alpaca client: positions and orders the live path reads."""

    def __init__(self, positions, fail_positions=False):
        self._positions = positions
        self._fail_positions = fail_positions

    def get_all_positions(self):
        if self._fail_positions:
            raise ConnectionError("Broker unreachable during outage")
        return self._positions

    def get_orders(self, *args, **kwargs):
        return []  # no protective orders, so stops/limits are empty


def _make_live_broker(fake_client):
    broker = AlpacaBroker(force_simulator=True)  # clean, no network
    broker.is_simulated = False
    broker.client = fake_client
    return broker


def _position(symbol, qty, entry, current, upnl):
    return types.SimpleNamespace(
        symbol=symbol,
        qty=qty,
        avg_entry_price=entry,
        current_price=current,
        unrealized_pl=upnl,
        unrealized_plpc=upnl / (entry * qty) if entry * qty else 0.0,
    )


def test_live_positions_reflect_broker_state():
    """The bot reconciles to the broker: displayed state == actual positions."""
    pos = _position("SPY", 10, 500.0, 505.0, 50.0)
    broker = _make_live_broker(_FakeClient([pos]))

    positions = broker.get_open_positions()

    assert "SPY" in positions
    assert positions["SPY"]["qty"] == 10
    assert positions["SPY"]["entry_price"] == 500.0
    assert positions["SPY"]["current_price"] == 505.0


def test_outage_degrades_to_empty_positions():
    """Characterization: on a broker outage the bot sees no positions.

    This is the current behaviour (get_open_positions returns {} on error rather
    than raising). It is safe against a crashed trade loop, but it means the bot
    *cannot distinguish* "no positions" from "broker is down" -- a real
    reconciliation risk the board flagged. Pinned so a regression is visible.
    """
    broker = _make_live_broker(_FakeClient([], fail_positions=True))
    assert broker.get_open_positions() == {}


def test_unreconciled_positions_block_new_bracket_order():
    broker = _make_live_broker(_FakeClient([], fail_positions=True))
    broker.audit_ledger = None
    broker.mandate_gate = None
    broker.paper_live_boundary = None
    broker.kill_switch = None
    broker.get_account_summary = lambda: {
        "equity": 100000.0, "cash": 100000.0,
        "buying_power": 200000.0, "mode": "ALPACA_PAPER",
    }

    result = broker.place_bracket_buy_order("SPY", 1, 500.0, 490.0, 520.0)

    assert not result.success
    assert "positions could not be reconciled" in result.message


def test_reconciliation_ignores_positions_without_protective_orders():
    """A live position with no protective orders still surfaces (stop=0)."""
    pos = _position("AAPL", 5, 200.0, 210.0, 50.0)
    broker = _make_live_broker(_FakeClient([pos]))
    positions = broker.get_open_positions()
    assert positions["AAPL"]["stop_loss"] == 0.0  # none attached; caller handles


def test_positions_reconciliation_ok_after_successful_fetch():
    """After a good fetch the position view is flagged as reconciled."""
    pos = _position("SPY", 10, 500.0, 505.0, 50.0)
    broker = _make_live_broker(_FakeClient([pos]))
    broker.get_open_positions()
    rec = broker.positions_reconciliation()
    assert rec["ok"] is True
    assert rec["error"] is None
    assert rec["source"] == "live"


def test_positions_reconciliation_flags_outage():
    """After a broker outage the position view is flagged untrusted.

    This is the observable signal that fixes the board's reconciliation concern:
    the bot can no longer confuse "broker down" with "genuinely flat".
    """
    broker = _make_live_broker(_FakeClient([], fail_positions=True))
    assert broker.get_open_positions() == {}
    rec = broker.positions_reconciliation()
    assert rec["ok"] is False
    assert rec["error"]  # the underlying cause is surfaced, not swallowed


def test_positions_reconciliation_always_ok_in_simulator():
    """The local simulator never has a broker round-trip, so it is always ok."""
    broker = AlpacaBroker(force_simulator=True)
    broker.sim_positions = {}
    broker.get_open_positions()
    rec = broker.positions_reconciliation()
    assert rec["ok"] is True
    assert rec["source"] == "simulator"
