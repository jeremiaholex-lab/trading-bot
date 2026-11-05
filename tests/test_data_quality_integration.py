import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from data.data_quality import (
    DataQualityGate,
    DataQualityConfig,
    QualityReport,
    QualityIssue,
    QualitySeverity,
    QualityCheck,
    validate_data_quality,
    should_skip_symbol,
)
from data.market_data import MarketDataProvider
from config import Config


def _create_sample_df(n_bars=60, freq_minutes=15, bad_ohlc=False, bad_zscore=False):
    base = datetime.now(timezone.utc) - timedelta(minutes=n_bars * freq_minutes)
    dates = [base + timedelta(minutes=i * freq_minutes) for i in range(n_bars)]
    
    close = np.linspace(100, 110, n_bars)
    if bad_zscore:
        close[30] = 500.0  # huge outlier
    open_p = close - 0.5
    high = close + 1.0
    low = close - 1.0
    if bad_ohlc:
        high[10] = low[10] - 5.0  # High < Low violation
    vol = np.full(n_bars, 1000.0)

    df = pd.DataFrame({
        "Open": open_p,
        "High": high,
        "Low": low,
        "Close": close,
        "Volume": vol,
    }, index=pd.to_datetime(dates))
    return df


def test_quality_report_should_skip_clean_data():
    df = _create_sample_df(60)
    report = validate_data_quality(df, "AAPL", "15Min")
    assert not report.has_critical()
    skip, reason = report.should_skip()
    assert skip is False
    assert reason == ""
    assert report.quality_score > 0.8


def test_quality_report_should_skip_critical_ohlc():
    df = _create_sample_df(60, bad_ohlc=True)
    report = validate_data_quality(df, "AAPL", "15Min")
    assert report.has_critical()
    skip, reason = should_skip_symbol(report)
    assert skip is True
    assert "Critical" in reason or "OHLC" in reason


def test_quality_report_low_score():
    report = QualityReport(symbol="XYZ", timeframe="15Min", total_rows=100)
    report.quality_score = 0.4
    skip, reason = report.should_skip(min_score=0.6)
    assert skip is True
    assert "score too low" in reason


def test_market_data_provider_quality_report():
    provider = MarketDataProvider()
    df = _create_sample_df(60)

    with patch.object(provider, "_fetch_bars_raw", return_value=df):
        bars = provider.get_historical_bars("AAPL", timeframe="15Min", days_back=2)
        assert not bars.empty
        rep = provider.get_quality_report("AAPL")
        assert rep is not None
        assert rep.symbol == "AAPL"
        assert rep.quality_score > 0.8


def test_stale_quotes_naive_and_aware_datetimes():
    gate = DataQualityGate()
    # Naive index using timedelta
    base = datetime(2026, 11, 1, 10, 0)
    dates = [base + timedelta(minutes=i * 15) for i in range(10)]
    df_naive = pd.DataFrame({
        "Open": [10.0] * 10,
        "High": [11.0] * 10,
        "Low": [9.0] * 10,
        "Close": [10.0] * 10,
        "Volume": [100.0] * 10,
    }, index=pd.to_datetime(dates))

    issues = gate.check_stale_quotes(df_naive, "TEST")
    assert len(issues) > 0
