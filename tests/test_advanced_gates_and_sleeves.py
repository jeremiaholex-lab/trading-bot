import pytest
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import numpy as np

from risk.macro_calendar_filter import MacroCalendarFilter
from risk.spread_filter import BidAskSpreadFilter
from execution.trade_journal import TradeJournal
from strategy.mean_reversion_sleeve import MeanReversionSleeve
from strategy.adaptive_strategy import AdaptiveStrategy, StrategyParameters
from indicators.technicals import TechnicalIndicators

def test_macro_calendar_blackout():
    # Setup custom event at a specific time
    event_time = datetime(2026, 6, 18, 18, 0, tzinfo=timezone.utc)
    custom_events = [{"name": "FOMC Rate Decision", "time_utc": event_time, "impact": "HIGH"}]

    mcf = MacroCalendarFilter(blackout_minutes_before=30, blackout_minutes_after=30, custom_events=custom_events)

    # 10 mins before event -> should be in blackout
    t_before = event_time - timedelta(minutes=10)
    in_blackout, reason = mcf.is_in_macro_blackout(t_before)
    assert in_blackout is True
    assert "FOMC Rate Decision" in reason

    # 45 mins before event -> clear
    t_outside = event_time - timedelta(minutes=45)
    in_blackout_outside, _ = mcf.is_in_macro_blackout(t_outside)
    assert in_blackout_outside is False

def test_bid_ask_spread_filter():
    sf = BidAskSpreadFilter(max_spread_pct=0.0015) # 0.15%

    # Tight spread: 500.00 / 500.20 -> 0.04% -> allowed
    ok, _ = sf.validate_spread("SPY", 500.00, 500.20)
    assert ok is True

    # Wide blown-out spread: 100.00 / 100.50 -> 0.50% -> rejected
    ok_wide, reason_wide = sf.validate_spread("AAPL", 100.00, 100.50)
    assert ok_wide is False
    assert "exceeding maximum permitted" in reason_wide

def test_offline_trade_journal_sync(tmp_path):
    db_file = tmp_path / "test_trades.db"
    tj = TradeJournal(db_path=db_file)

    # Record an entry
    tj.record_entry("AAPL", 10, 150.0, stop_loss=145.0, take_profit=160.0, reason="Test entry")

    # Mock broker reporting a simulated exit while bot was stopped
    class MockBroker:
        is_simulated = True
        last_simulated_exit_details = [
            {"symbol": "AAPL", "exit_price": 160.0, "reason": "Take Profit hit offline"}
        ]
        def get_open_positions(self):
            return {}

    synced = tj.sync_broker_filled_orders(MockBroker())
    assert synced == 1

    # Verify trade in db is now CLOSED
    recent = tj.get_recent_trades(5)
    assert recent[0]["symbol"] == "AAPL"
    assert recent[0]["status"] == "CLOSED"
    assert recent[0]["exit_price"] == 160.0

def test_mean_reversion_sleeve_signals():
    sleeve = MeanReversionSleeve(rsi_oversold_threshold=35.0, rsi_overbought_threshold=65.0)

    # Create synthetic DataFrame where price dips into lower Bollinger band with oversold RSI
    n = 60
    close = [100.0] * n
    # Make last 2 bars dip down and bounce
    close[-3] = 95.0
    close[-2] = 92.0
    close[-1] = 92.5 # slight bounce

    df = pd.DataFrame({
        "Open": close,
        "High": [c + 1.0 for c in close],
        "Low": [c - 1.0 for c in close],
        "Close": close,
        "Volume": [100000] * n
    })

    df_ind = TechnicalIndicators.add_all_indicators(df)
    action, conf, reason, meta = sleeve.evaluate_mean_reversion_setup("SPY", df_ind, atr=1.5)

    assert action in ("BUY", "HOLD")
    if action == "BUY":
        assert conf >= 0.70
        assert "Mean-Reversion Sleeve" in reason
