import pytest
from config import Config
from risk.risk_manager import RiskManager, PositionSizeResult
from strategy.adaptive_strategy import TradeSignal

@pytest.fixture
def risk_mgr():
    # The real EarningsFilter fetches live yfinance data (network). These
    # tests are hermetic, so inject a no-op that never blocks. Earnings
    # blackout behaviour is covered separately in test_advanced_risk_gates.py.
    class _NoopEarnings:
        def is_in_blackout(self, symbol, blackout_days=2, current_date=None):
            return False, ""
    rm = RiskManager(earnings_filter=_NoopEarnings())
    rm.update_start_of_day_equity(100000.0)
    return rm

@pytest.fixture
def sample_buy_signal():
    return TradeSignal(
        action="BUY",
        symbol="AAPL",
        entry_price=150.0,
        stop_loss_price=145.0,  # $5 risk per share
        take_profit_price=162.5,
        confidence=0.80,
        regime="TRENDING_BULL",
        reason="EMA Pullback confirmation",
        metadata={}
    )

def test_circuit_breaker_tripped(risk_mgr):
    # Starting equity is 100,000. Drawdown limit is 3% ($97,000).
    # If equity drops to 96,000, circuit breaker must trip.
    tripped = risk_mgr.check_circuit_breaker(96000.0)
    assert tripped is True
    assert risk_mgr.circuit_breaker_tripped is True

def test_circuit_breaker_stays_latched_after_equity_recovers(risk_mgr):
    assert risk_mgr.check_circuit_breaker(96000.0) is True
    assert risk_mgr.check_circuit_breaker(100500.0) is True
    assert risk_mgr.circuit_breaker_tripped is True


def test_circuit_breaker_blocks_trading(risk_mgr, sample_buy_signal):
    risk_mgr.check_circuit_breaker(96000.0)
    res = risk_mgr.calculate_position_size(
        signal=sample_buy_signal,
        portfolio_equity=96000.0,
        cash_available=50000.0,
        current_open_positions_count=0,
        already_holding_symbol=False
    )
    assert res.allowed is False
    assert "circuit breaker" in res.rejection_reason.lower()

def test_daily_breaker_baseline_and_latch_survive_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(RiskManager, "_today_et", staticmethod(lambda: "2099-01-02"))
    state_path = tmp_path / "risk_state.json"
    first = RiskManager(state_path=state_path)
    first.update_start_of_day_equity(100000.0, session_date="2099-01-02")
    assert first.check_circuit_breaker(96000.0) is True

    restored = RiskManager(state_path=state_path)
    restored.update_start_of_day_equity(105000.0, session_date="2099-01-02")
    assert restored.start_of_day_equity == 100000.0
    assert restored.check_circuit_breaker(105000.0) is True


def test_corrupt_same_day_risk_state_blocks_entries(tmp_path, monkeypatch):
    state_path = tmp_path / "risk_state.json"
    state_path.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(RiskManager, "_today_et", staticmethod(lambda: "2099-01-02"))
    risk_mgr = RiskManager(state_path=state_path)
    risk_mgr.update_start_of_day_equity(100000.0, session_date="2099-01-02")

    assert risk_mgr.check_circuit_breaker(100000.0) is True


def test_breaker_state_write_failure_fails_closed(tmp_path):
    blocked_parent = tmp_path / "not_a_directory"
    blocked_parent.write_text("block", encoding="utf-8")
    risk_mgr = RiskManager(state_path=blocked_parent / "risk_state.json")
    risk_mgr.update_start_of_day_equity(100000.0, session_date="2099-01-02")
    assert risk_mgr.check_circuit_breaker(100000.0) is True


def test_position_sizing_within_risk_cap(risk_mgr, sample_buy_signal):
    # 1.5% max risk on 100,000 equity = $1,500
    # $5 risk per share -> 300 shares max
    res = risk_mgr.calculate_position_size(
        signal=sample_buy_signal,
        portfolio_equity=100000.0,
        cash_available=80000.0,
        current_open_positions_count=1,
        already_holding_symbol=False
    )
    assert res.allowed is True
    assert res.shares > 0
    assert res.risk_amount <= 100000.0 * 0.015 + 1e-5

def test_max_positions_rejection(risk_mgr, sample_buy_signal):
    res = risk_mgr.calculate_position_size(
        signal=sample_buy_signal,
        portfolio_equity=100000.0,
        cash_available=80000.0,
        current_open_positions_count=risk_mgr.max_positions,
        already_holding_symbol=False
    )
    assert res.allowed is False
    assert "Max concurrent positions" in res.rejection_reason

def test_duplicate_holding_rejection(risk_mgr, sample_buy_signal):
    res = risk_mgr.calculate_position_size(
        signal=sample_buy_signal,
        portfolio_equity=100000.0,
        cash_available=80000.0,
        current_open_positions_count=1,
        already_holding_symbol=True
    )
    assert res.allowed is False
    assert "already open" in res.rejection_reason

def test_news_gate_blocks_trade(risk_mgr, sample_buy_signal, monkeypatch):
    """The breaking-news gate rejects when the news filter reports risk."""
    monkeypatch.setattr(Config, "ENABLE_NEWS_FILTER", True)

    class BlockingNews:
        def is_blocked(self, symbol, hours=None, important_only=None, now=None):
            return True, "News risk (legal risk): probe opened"
    risk_mgr.news_filter = BlockingNews()
    res = risk_mgr.calculate_position_size(
        signal=sample_buy_signal,
        portfolio_equity=100000.0,
        cash_available=80000.0,
        current_open_positions_count=0,
        already_holding_symbol=False
    )
    assert res.allowed is False
    assert "News Risk" in res.rejection_reason

def test_news_gate_allows_when_clear(risk_mgr, sample_buy_signal, monkeypatch):
    """A clear news filter must not block a valid trade."""
    monkeypatch.setattr(Config, "ENABLE_NEWS_FILTER", True)

    class ClearNews:
        def is_blocked(self, symbol, hours=None, important_only=None, now=None):
            return False, ""
    risk_mgr.news_filter = ClearNews()
    res = risk_mgr.calculate_position_size(
        signal=sample_buy_signal,
        portfolio_equity=100000.0,
        cash_available=80000.0,
        current_open_positions_count=0,
        already_holding_symbol=False
    )
    assert res.allowed is True
