import json
import numpy as np
import pandas as pd
import pytest
from pathlib import Path
from optimizer.backtester import StrategyBacktester, BacktestResult
from optimizer.self_improver import SelfImprover
from strategy.adaptive_strategy import StrategyParameters

@pytest.fixture
def sample_market_data():
    np.random.seed(123)
    n = 120
    dates = pd.date_range("2026-01-01", periods=n, freq="15min")
    close = 100.0 + np.cumsum(np.random.normal(0.2, 0.8, n))
    high = close + 1.0
    low = close - 1.0
    open_ = close + np.random.normal(0, 0.2, n)
    volume = np.random.randint(5000, 20000, n)
    return pd.DataFrame({
        "Open": open_,
        "High": high,
        "Low": low,
        "Close": close,
        "Volume": volume
    }, index=dates)

def test_backtester_runs_and_returns_result(sample_market_data):
    params = StrategyParameters()
    result = StrategyBacktester.run_backtest(sample_market_data, params, symbol="SPY")
    assert isinstance(result, BacktestResult)
    assert hasattr(result, "win_rate")
    assert hasattr(result, "profit_factor")
    assert hasattr(result, "sharpe_ratio")
    assert hasattr(result, "max_drawdown_pct")

def test_self_improver_atomic_save_and_load(tmp_path):
    test_state_file = tmp_path / "test_strategy_state.json"
    improver = SelfImprover(state_file=test_state_file)

    assert improver.state.get("version") == 1
    improver.state["symbols"]["TEST"] = {
        "params": StrategyParameters(ema_fast=7, ema_slow=19).to_dict()
    }
    improver._save_state()

    assert test_state_file.exists()
    # Check that temp file was cleaned up by atomic rename
    assert not test_state_file.with_suffix(".tmp").exists()

    # Re-load
    improver2 = SelfImprover(state_file=test_state_file)
    params = improver2.get_symbol_params("TEST")
    assert params.ema_fast == 7
    assert params.ema_slow == 19

def test_self_improver_corrupted_json_recovery(tmp_path):
    corrupted_file = tmp_path / "corrupted_state.json"
    corrupted_file.write_text("{this is not valid json!}", encoding="utf-8")

    improver = SelfImprover(state_file=corrupted_file)
    assert improver.state["version"] == 1
    assert improver.state["symbols"] == {}
