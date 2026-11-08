"""Backtester: shared entry logic with the live strategy, and cost modelling."""
import numpy as np
import pandas as pd
import pytest

from optimizer.backtester import StrategyBacktester, CostModel, SIGNAL_WINDOW_BARS
from strategy.adaptive_strategy import (
    AdaptiveStrategy, StrategyParameters, MIN_ENTRY_CONFIDENCE,
)
from indicators.technicals import TechnicalIndicators


def _market(n=1500, seed=123, drift=0.0001):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-02 14:30", periods=n, freq="15min", tz="UTC")
    c = 100 * np.exp(np.cumsum(rng.normal(drift, 0.004, n)))
    return pd.DataFrame({
        "Open": np.r_[c[0], c[:-1]], "High": c * 1.003, "Low": c * 0.997,
        "Close": c, "Volume": rng.integers(100_000, 200_000, n),
    }, index=idx)


def test_windowed_signal_equals_full_history_signal():
    """A trailing window must give the same signal as the whole history."""
    df = _market(400)
    s = AdaptiveStrategy(StrategyParameters())
    ind = TechnicalIndicators.add_all_indicators(df)
    for i in range(60, 400, 7):
        full = s.signal_from_indicators("X", ind.iloc[: i + 1])
        win = s.signal_from_indicators("X", ind.iloc[max(0, i - SIGNAL_WINDOW_BARS + 1): i + 1])
        assert (full.action, full.confidence, full.stop_loss_price, full.take_profit_price) == \
               (win.action, win.confidence, win.stop_loss_price, win.take_profit_price)


def test_generate_signal_matches_signal_from_indicators():
    df = _market(300)
    s = AdaptiveStrategy(StrategyParameters())
    ind = TechnicalIndicators.add_all_indicators(df)
    assert s.generate_signal("X", df) == s.signal_from_indicators("X", ind)


def test_risk_manager_uses_shared_confidence_constant():
    from risk import risk_manager
    assert risk_manager.MIN_ENTRY_CONFIDENCE is MIN_ENTRY_CONFIDENCE


def test_cost_model_math():
    cm = CostModel(slippage_bps=10, commission_bps=2, commission_per_share=0.0, exit_slippage=True)
    assert cm.buy_fill(100) == pytest.approx(100.10)
    assert cm.sell_fill(100, market=True) == pytest.approx(99.90)
    assert cm.sell_fill(100, market=False) == 100  # resting limit: no slippage
    assert cm.commission_pct(100) == pytest.approx(0.0002)
    assert CostModel(0, 0, 0.01, True).commission_pct(50) == pytest.approx(0.0002)
    z = CostModel.zero()
    assert z.buy_fill(100) == 100 and z.sell_fill(100, True) == 100 and z.commission_pct(100) == 0


def test_exit_slippage_can_be_disabled():
    cm = CostModel(slippage_bps=10, exit_slippage=False)
    assert cm.sell_fill(100, market=True) == 100


def test_costs_reduce_returns_monotonically():
    df = _market()
    p = StrategyParameters()
    runs = [StrategyBacktester.run_backtest(df, p, costs=c) for c in (
        CostModel.zero(), CostModel(5, 1, 0, True), CostModel(20, 5, 0, True))]
    assert runs[0].total_trades == runs[1].total_trades == runs[2].total_trades > 0
    assert runs[0].total_pnl_pct > runs[1].total_pnl_pct > runs[2].total_pnl_pct
    assert runs[0].cost_drag_pct == 0
    assert 0 < runs[1].cost_drag_pct < runs[2].cost_drag_pct


def test_zero_cost_run_reports_no_drag():
    r = StrategyBacktester.run_backtest(_market(), StrategyParameters(), costs=CostModel.zero())
    assert r.cost_drag_pct == 0.0


def test_config_defaults_used_when_costs_not_given():
    df = _market()
    p = StrategyParameters()
    a = StrategyBacktester.run_backtest(df, p)
    b = StrategyBacktester.run_backtest(df, p, costs=CostModel.from_config())
    assert a == b


def test_negative_cost_config_rejected(monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "BACKTEST_SLIPPAGE_BPS", -1.0)
    errors = Config.validate()
    assert any("BACKTEST_SLIPPAGE_BPS" in e for e in errors)
