import math
import pytest
from datetime import datetime, timezone, time as dtime
from execution.simulator import (
    SlippageModel,
    LatencyModel,
    FillEngine,
    SlippageResult,
    LatencyResult,
    FillResult,
    _half_spread,
    _market_hours_multiplier,
)
from execution.alpaca_broker import AlpacaBroker, SimulatedPosition


def test_half_spread_known_large_caps():
    assert _half_spread(None, "SPY") == 0.0001
    assert _half_spread(None, "AAPL") == 0.0001
    assert _half_spread(None, "NVDA") == 0.0001


def test_half_spread_by_adv():
    assert _half_spread(150_000_000, "XYZ") == 0.0001
    assert _half_spread(50_000_000, "XYZ") == 0.0004
    assert _half_spread(1_000_000, "XYZ") == 0.0012
    assert _half_spread(None, "UNKNOWN") == 0.0004


def test_slippage_buy_direction():
    model = SlippageModel(impact_factor=0.1, min_bps=1.0, max_bps=100.0, seed=42)
    res = model.compute(price=100.0, qty=10, side="buy", symbol="AAPL")
    assert res.fill_price > 100.0
    assert res.total_bps >= 1.0
    assert res.side == "buy"
    assert "fill=" in res.log_line()


def test_slippage_sell_direction():
    model = SlippageModel(impact_factor=0.1, min_bps=1.0, max_bps=100.0, seed=42)
    res = model.compute(price=100.0, qty=10, side="sell", symbol="AAPL")
    assert res.fill_price < 100.0
    assert res.total_bps >= 1.0
    assert res.side == "sell"


def test_slippage_market_impact_scaling():
    model = SlippageModel(impact_factor=0.2, min_bps=0.0, max_bps=500.0, seed=123)
    res_small = model.compute(price=100.0, qty=10, side="buy", adv_usd=10_000_000, symbol="TEST")
    res_large = model.compute(price=100.0, qty=100_000, side="buy", adv_usd=10_000_000, symbol="TEST")
    assert res_large.impact_cost > res_small.impact_cost
    assert res_large.total_bps > res_small.total_bps


def test_slippage_volatility_regime():
    model = SlippageModel(impact_factor=0.1, min_bps=0.0, max_bps=500.0, seed=123)
    res_low_vol = model.compute(price=100.0, qty=10, side="buy", atr=0.005, symbol="AAPL")
    res_high_vol = model.compute(price=100.0, qty=10, side="buy", atr=0.05, symbol="AAPL")
    assert res_high_vol.vol_cost > res_low_vol.vol_cost
    assert res_high_vol.total_bps > res_low_vol.total_bps


def test_slippage_zero_qty_or_price():
    model = SlippageModel()
    res = model.compute(price=0.0, qty=10, side="buy")
    assert res.fill_price == 0.0
    res2 = model.compute(price=100.0, qty=0, side="buy")
    assert res2.fill_price == 100.0


def test_latency_model_non_blocking():
    lat = LatencyModel(mean_ms=20.0, sigma_ms=5.0, fill_mean_ms=10.0, block=False, seed=42)
    res = lat.simulate()
    assert res.total_ms > 0
    assert res.submission_ms >= 0
    assert res.fill_ms >= 0
    assert "latency=" in res.log_line()


def test_market_hours_multiplier():
    mult, near_open, near_close = _market_hours_multiplier(dtime(9, 35))
    assert mult == 1.5
    assert near_open is True

    mult, near_open, near_close = _market_hours_multiplier(dtime(15, 50))
    assert mult == 1.5
    assert near_close is True

    mult, near_open, near_close = _market_hours_multiplier(dtime(12, 0))
    assert mult == 1.0
    assert near_open is False
    assert near_close is False


def test_fill_engine_from_config():
    engine = FillEngine.from_config(block_latency=False)
    res = engine.fill(price=150.0, qty=50, side="buy", symbol="MSFT")
    assert isinstance(res, FillResult)
    assert res.fill_price > 150.0
    assert res.total_latency_ms >= 0
    assert "fill=" in res.log_line()


def test_broker_simulator_bracket_buy_slippage(tmp_path):
    state_file = tmp_path / "sim_state.json"
    broker = AlpacaBroker(force_simulator=True, state_file=state_file)
    broker.fill_engine.latency.block = False  # fast tests

    res = broker.place_bracket_buy_order("AAPL", 10, 150.0, 140.0, 170.0)
    assert res.success is True
    assert res.entry_price > 150.0  # paid slippage
    pos = broker.sim_positions["AAPL"]
    assert pos.entry_price == res.entry_price


def test_broker_simulator_close_position_slippage(tmp_path, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "SIM_EXIT_SLIPPAGE", True)
    state_file = tmp_path / "sim_state.json"
    broker = AlpacaBroker(force_simulator=True, state_file=state_file)
    broker.fill_engine.latency.block = False

    # Seed position
    broker.sim_positions["AAPL"] = SimulatedPosition(
        symbol="AAPL",
        qty=10,
        entry_price=150.0,
        current_price=160.0,
        stop_loss=140.0,
        take_profit=180.0,
        entry_time="2026-11-05T00:00:00Z",
    )
    initial_cash = broker.sim_cash
    res = broker.place_close_position_order("AAPL", 10, reason="Take profit test")
    assert res.success is True
    assert "AAPL" not in broker.sim_positions
    # Proceeds added to cash
    assert broker.sim_cash > initial_cash
