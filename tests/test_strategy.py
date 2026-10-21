import numpy as np
import pandas as pd
import pytest
from strategy.adaptive_strategy import AdaptiveStrategy, StrategyParameters, TradeSignal

@pytest.fixture
def mock_bars():
    n = 100
    dates = pd.date_range("2026-01-01", periods=n, freq="15min")
    close = 150.0 + np.linspace(0, 10, n)
    return pd.DataFrame({
        "Open": close - 0.2,
        "High": close + 0.5,
        "Low": close - 0.5,
        "Close": close,
        "Volume": np.full(n, 10000)
    }, index=dates)

def test_strategy_parameters_serialization():
    params = StrategyParameters(
        ema_fast=10,
        ema_slow=30,
        rsi_buy_threshold=45.0,
        atr_stop_multiplier=2.0
    )
    p_dict = params.to_dict()
    assert p_dict["ema_fast"] == 10
    assert p_dict["ema_slow"] == 30
    assert p_dict["atr_stop_multiplier"] == 2.0

    restored = StrategyParameters.from_dict(p_dict)
    assert restored.ema_fast == 10
    assert restored.ema_slow == 30
    assert restored.atr_stop_multiplier == 2.0

def test_insufficient_data_returns_hold():
    strategy = AdaptiveStrategy()
    short_df = pd.DataFrame({
        "Open": [100.0],
        "High": [101.0],
        "Low": [99.0],
        "Close": [100.5],
        "Volume": [500]
    })
    signal = strategy.generate_signal("SPY", short_df)
    assert signal.action == "HOLD"
    assert signal.confidence == 0.0
    assert "Insufficient data" in signal.reason

def test_signal_generation_bracket_math(mock_bars):
    strategy = AdaptiveStrategy()
    signal = strategy.generate_signal("SPY", mock_bars)

    assert isinstance(signal, TradeSignal)
    assert signal.symbol == "SPY"
    if signal.action == "BUY":
        assert signal.stop_loss_price < signal.entry_price
        assert signal.take_profit_price > signal.entry_price
        risk = signal.entry_price - signal.stop_loss_price
        reward = signal.take_profit_price - signal.entry_price
        assert reward / risk >= 1.5
