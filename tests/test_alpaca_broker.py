import pytest
from execution.alpaca_broker import AlpacaBroker, retry_api_call, OrderResult

# NOTE: these tests use `force_simulator=True` rather than poking at broker
# internals. That keeps them hermetic -- they no longer depend on whatever
# credentials happen to be in the developer's .env, and can never reach the
# real Alpaca API.

def test_simulator_initial_balance():
    broker = AlpacaBroker(force_simulator=True)
    broker.sim_cash = 100000.0
    broker.sim_positions = {}

    summary = broker.get_account_summary()
    assert summary["equity"] == 100000.0
    assert summary["cash"] == 100000.0
    assert "LOCAL PAPER SIMULATOR" in summary["mode"]

def test_simulator_place_bracket_buy_order():
    broker = AlpacaBroker(force_simulator=True)
    broker.sim_cash = 100000.0
    broker.sim_positions = {}

    res = broker.place_bracket_buy_order(
        symbol="SPY",
        qty=10,
        entry_price=500.0,
        stop_loss_price=490.0,
        take_profit_price=520.0
    )
    assert res.success is True
    assert res.symbol == "SPY"
    assert broker.sim_cash == 100000.0 - 5000.0
    assert "SPY" in broker.sim_positions

def test_simulator_stop_loss_execution():
    broker = AlpacaBroker(force_simulator=True)
    broker.sim_cash = 95000.0
    broker.sim_positions = {}

    broker.place_bracket_buy_order(
        symbol="AAPL",
        qty=10,
        entry_price=200.0,
        stop_loss_price=190.0,
        take_profit_price=220.0
    )
    # Price falls to 189 (below stop_loss 190)
    exits = broker.update_simulated_prices({"AAPL": 189.0})
    assert len(exits) == 1
    assert "STOP LOSS TRIGGERED" in exits[0]
    assert "AAPL" not in broker.sim_positions

def test_simulator_take_profit_execution():
    broker = AlpacaBroker(force_simulator=True)
    broker.sim_cash = 95000.0
    broker.sim_positions = {}

    broker.place_bracket_buy_order(
        symbol="NVDA",
        qty=10,
        entry_price=100.0,
        stop_loss_price=95.0,
        take_profit_price=110.0
    )
    # Price rises to 112 (above take_profit 110)
    exits = broker.update_simulated_prices({"NVDA": 112.0})
    assert len(exits) == 1
    assert "TAKE PROFIT HIT" in exits[0]
    assert "NVDA" not in broker.sim_positions

def test_retry_api_call_succeeds_first_try():
    call_count = 0
    def sample_func():
        nonlocal call_count
        call_count += 1
        return "success"

    result = retry_api_call(sample_func, max_retries=3, base_delay=0.01)
    assert result == "success"
    assert call_count == 1

def test_retry_api_call_retries_and_recovers():
    call_count = 0
    def transient_func():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise ConnectionError("Temporary timeout")
        return "recovered"

    result = retry_api_call(transient_func, max_retries=3, base_delay=0.01)
    assert result == "recovered"
    assert call_count == 2

def test_retry_api_call_exhausts_retries():
    call_count = 0
    def failing_func():
        nonlocal call_count
        call_count += 1
        raise ConnectionError("Persistent server failure")

    with pytest.raises(ConnectionError):
        retry_api_call(failing_func, max_retries=2, base_delay=0.01)
    assert call_count == 2
