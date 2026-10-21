import pytest
from risk.risk_manager import RiskManager, PositionSizeResult
from strategy.adaptive_strategy import TradeSignal

@pytest.fixture
def risk_mgr():
    rm = RiskManager()
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
