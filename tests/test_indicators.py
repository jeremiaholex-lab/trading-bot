import numpy as np
import pandas as pd
import pytest
from indicators.technicals import TechnicalIndicators

@pytest.fixture
def sample_ohlcv_df():
    """Generates synthetic price data for testing technical indicators."""
    np.random.seed(42)
    n = 100
    dates = pd.date_range("2026-01-01", periods=n, freq="15min")
    base_price = 100.0 + np.cumsum(np.random.normal(0.1, 1.0, n))
    highs = base_price + np.random.uniform(0.5, 2.0, n)
    lows = base_price - np.random.uniform(0.5, 2.0, n)
    opens = base_price + np.random.uniform(-0.5, 0.5, n)
    closes = base_price
    volumes = np.random.randint(1000, 10000, n)

    return pd.DataFrame({
        "Open": opens,
        "High": highs,
        "Low": lows,
        "Close": closes,
        "Volume": volumes
    }, index=dates)

def test_add_all_indicators(sample_ohlcv_df):
    """Enriched DataFrame should contain all required indicator columns."""
    df = TechnicalIndicators.add_all_indicators(sample_ohlcv_df, ema_fast=9, ema_slow=21)
    
    expected_cols = [
        "EMA_9", "EMA_21", "EMA_50", "EMA_200",
        "RSI", "ATR", "ATR_Pct", "BB_Mid", "BB_Upper", "BB_Lower", "BB_Width",
        "MACD", "MACD_Signal", "MACD_Hist", "Volume_SMA20", "Volume_Ratio", "Regime"
    ]
    for col in expected_cols:
        assert col in df.columns, f"Missing column {col}"

def test_rsi_bounds(sample_ohlcv_df):
    """RSI values should be bounded between 0 and 100."""
    df = TechnicalIndicators.add_all_indicators(sample_ohlcv_df)
    valid_rsi = df["RSI"].dropna()
    assert (valid_rsi >= 0).all()
    assert (valid_rsi <= 100).all()

def test_atr_positive(sample_ohlcv_df):
    """ATR should always be strictly positive."""
    df = TechnicalIndicators.add_all_indicators(sample_ohlcv_df)
    valid_atr = df["ATR"].dropna()
    assert (valid_atr > 0).all()

def test_bollinger_bands_ordering(sample_ohlcv_df):
    """Upper BB should always be greater than Mid, and Mid greater than Lower."""
    df = TechnicalIndicators.add_all_indicators(sample_ohlcv_df)
    valid_bb = df[["BB_Upper", "BB_Mid", "BB_Lower"]].dropna()
    assert (valid_bb["BB_Upper"] >= valid_bb["BB_Mid"]).all()
    assert (valid_bb["BB_Mid"] >= valid_bb["BB_Lower"]).all()

def test_regime_labels(sample_ohlcv_df):
    """Regime classifications should belong to allowed regime set."""
    df = TechnicalIndicators.add_all_indicators(sample_ohlcv_df)
    allowed_regimes = {"TRENDING_BULL", "TRENDING_BEAR", "RANGING", "HIGH_VOLATILITY"}
    unique_regimes = set(df["Regime"].unique())
    assert unique_regimes.issubset(allowed_regimes)
