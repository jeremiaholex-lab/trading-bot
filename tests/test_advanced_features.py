import pytest
import pandas as pd
import numpy as np
from execution.webhook import WebhookNotifier
from strategy.scanner import AssetScanner
from indicators.technicals import TechnicalIndicators

def test_webhook_notifier_disabled(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    notifier = WebhookNotifier()
    assert not notifier.enable_discord
    assert not notifier.enable_telegram
    # Should not raise exception when sending alert with no webhooks configured
    notifier.send_alert("Test", "Hello", level="INFO")

def test_asset_scanner():
    dates = pd.date_range(start="2025-01-01", periods=100, freq="15min")
    np.random.seed(42)
    prices = 100 + np.cumsum(np.random.randn(100) * 0.5)
    highs = prices + np.random.rand(100) * 0.5
    lows = prices - np.random.rand(100) * 0.5
    volumes = np.random.randint(1000, 5000, size=100)

    df = pd.DataFrame({
        "Open": prices,
        "High": highs,
        "Low": lows,
        "Close": prices,
        "Volume": volumes
    }, index=dates)

    scanner = AssetScanner(["SPY", "QQQ"])
    market_data = {
        "SPY": df,
        "QQQ": df.copy()
    }

    ranked = scanner.scan_and_rank(market_data)
    assert isinstance(ranked, list)
    assert len(ranked) == 2
    assert "symbol" in ranked[0]
    assert "score" in ranked[0]
    assert ranked[0]["score"] >= ranked[1]["score"]

def test_technical_indicators_vwap_and_mtf():
    dates = pd.date_range(start="2025-01-01", periods=60, freq="15min")
    prices = np.linspace(100, 110, 60)
    df = pd.DataFrame({
        "Open": prices,
        "High": prices + 1,
        "Low": prices - 1,
        "Close": prices,
        "Volume": [1000] * 60
    }, index=dates)

    df_ind = TechnicalIndicators.add_all_indicators(df)
    assert "VWAP" in df_ind.columns
    assert "Macro_Trend" in df_ind.columns
    assert "MTF_Bullish" in df_ind.columns
    assert not df_ind["VWAP"].isna().all()
