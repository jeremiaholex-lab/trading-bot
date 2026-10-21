"""Regression tests for the live trading loop.

These cover the code path that actually places orders. The bugs here were
invisible to the unit tests because they live in the *orchestration* layer:
state read at the wrong time, signals generated but never executed, and
promised features that were never wired up.
"""
import numpy as np
import pandas as pd
import pytest

from config import Config
from risk.risk_manager import RiskManager
from strategy.adaptive_strategy import TradeSignal


# ----------------------------------------------------------------------
# A minimal, fully in-memory stand-in for the broker. It records every
# order so the loop's behaviour can be asserted without touching Alpaca.
# ----------------------------------------------------------------------
class FakeBroker:
    def __init__(self, account=None, positions=None, clock=None):
        self._account = account or {
            "equity": 100000.0, "cash": 100000.0, "buying_power": 200000.0,
            "mode": "TEST", "currency": "USD",
        }
        self._positions = positions or {}
        self._clock = clock or {"is_open": True, "next_open": None, "next_close": None}
        self.buy_orders = []
        self.sell_orders = []
        self.exits = []
        self.trailing_stop_updates = []

    def get_account_summary(self):
        return dict(self._account)

    def get_open_positions(self):
        return {k: dict(v) for k, v in self._positions.items()}

    def get_market_clock(self):
        return dict(self._clock)

    def place_bracket_buy_order(self, symbol, qty, entry_price, stop_loss_price, take_profit_price):
        self.buy_orders.append(
            {"symbol": symbol, "qty": qty, "entry": entry_price,
             "stop": stop_loss_price, "target": take_profit_price}
        )
        self._positions[symbol] = {
            "symbol": symbol, "qty": qty, "entry_price": entry_price,
            "current_price": entry_price, "unrealized_pl": 0.0, "unrealized_plpc": 0.0,
        }
        return _ok_order(symbol, qty, entry_price, stop_loss_price, take_profit_price)

    def place_close_position_order(self, symbol, qty, reason=""):
        self.sell_orders.append({"symbol": symbol, "qty": qty, "reason": reason})
        self._positions.pop(symbol, None)
        return _ok_order(symbol, qty, 0.0, 0.0, 0.0)

    def update_simulated_prices(self, price_map):
        self.exits.append(dict(price_map))
        return []

    def update_trailing_stop(self, symbol, new_stop):
        self.trailing_stop_updates.append({"symbol": symbol, "new_stop": new_stop})
        return True


class _Order:
    def __init__(self, symbol, qty, entry, stop, target):
        self.success = True
        self.order_id = "FAKE-1"
        self.symbol = symbol
        self.qty = qty
        self.entry_price = entry
        self.stop_loss = stop
        self.take_profit = target
        self.message = "ok"


def _ok_order(symbol, qty, entry, stop, target):
    return _Order(symbol, qty, entry, stop, target)


def _bars(n=120, price=150.0):
    close = np.full(n, price)
    return pd.DataFrame(
        {
            "Open": close, "High": close + 0.5, "Low": close - 0.5,
            "Close": close, "Volume": np.full(n, 10000),
        },
        index=pd.date_range("2026-01-01", periods=n, freq="15min"),
    )


def _signal(action, symbol, confidence=0.9, price=150.0):
    return TradeSignal(
        action=action,
        symbol=symbol,
        entry_price=price,
        stop_loss_price=price - 5.0,
        take_profit_price=price + 10.0,
        confidence=confidence,
        regime="TRENDING_BULL",
        reason="test",
        metadata={},
    )


class FakeMarketData:
    def __init__(self, frames=None):
        self.frames = frames or {}

    def get_historical_bars(self, symbol, timeframe="15Min", days_back=60):
        return self.frames.get(symbol, _bars())


def _make_orchestrator(monkeypatch, broker, signals, frames=None, watchlist=None):
    """Builds a TradingBotOrchestrator with every external dependency faked."""
    import bot as botmod
    from strategy.adaptive_strategy import AdaptiveStrategy

    monkeypatch.setattr(botmod, "AlpacaBroker", lambda: broker)
    monkeypatch.setattr(botmod, "MarketDataProvider", lambda: FakeMarketData(frames))

    if watchlist is not None:
        monkeypatch.setattr(botmod.Config, "WATCHLIST", watchlist)

    class ScriptedStrategy:
        def __init__(self, params=None):
            self.params = params

        def generate_signal(self, symbol, df):
            return signals[symbol] if symbol in signals else _signal("HOLD", symbol)

    monkeypatch.setattr(botmod, "AdaptiveStrategy", ScriptedStrategy)

    orch = botmod.TradingBotOrchestrator.__new__(botmod.TradingBotOrchestrator)
    orch.market_data = FakeMarketData(frames)
    orch.broker = broker
    orch.risk_mgr = RiskManager()
    orch.risk_mgr.update_start_of_day_equity(broker.get_account_summary()["equity"])
    orch.self_improver = type(
        "S", (), {"get_symbol_params": staticmethod(lambda s: None),
                  "state": {}}
    )()
    return orch


# ----------------------------------------------------------------------
# Bug: MAX_OPEN_POSITIONS was enforced against a snapshot of open
# positions taken BEFORE the scan loop, so every symbol in the loop saw
# the same count and the cap could be exceeded within a single scan.
# ----------------------------------------------------------------------
def test_position_cap_is_enforced_within_a_single_scan(monkeypatch):
    """A scan must not open more positions than MAX_OPEN_POSITIONS."""
    from config import Config

    broker = FakeBroker()
    watchlist = ["A", "B", "C", "D", "E", "F", "G"]
    signals = {sym: _signal("BUY", sym) for sym in watchlist}
    orch = _make_orchestrator(monkeypatch, broker, signals, watchlist=watchlist)

    orch.scan_and_trade()

    assert len(broker.buy_orders) <= Config.MAX_OPEN_POSITIONS, (
        f"opened {len(broker.buy_orders)} positions in one scan with a cap of "
        f"{Config.MAX_OPEN_POSITIONS}"
    )


def test_position_cap_counts_positions_opened_earlier_in_the_same_scan(monkeypatch):
    """The cap must be evaluated against live state, not a pre-scan snapshot."""
    from config import Config

    broker = FakeBroker()
    broker._positions["EXISTING"] = {
        "symbol": "EXISTING", "qty": 1, "entry_price": 100.0,
        "current_price": 100.0, "unrealized_pl": 0.0, "unrealized_plpc": 0.0,
    }
    watchlist = ["A", "B", "C", "D", "E", "F", "G"]
    signals = {sym: _signal("BUY", sym) for sym in watchlist}
    orch = _make_orchestrator(monkeypatch, broker, signals, watchlist=watchlist)

    orch.scan_and_trade()

    total_open = len(broker.get_open_positions())
    assert total_open <= Config.MAX_OPEN_POSITIONS, (
        f"{total_open} positions open after one scan, cap is {Config.MAX_OPEN_POSITIONS}"
    )


def test_position_cap_is_not_exceeded_by_a_single_scan(monkeypatch):
    """With no pre-existing positions the cap still holds across a scan."""
    from config import Config

    broker = FakeBroker()
    watchlist = [f"SYM{i}" for i in range(12)]
    signals = {sym: _signal("BUY", sym) for sym in watchlist}
    orch = _make_orchestrator(monkeypatch, broker, signals, watchlist=watchlist)

    orch.scan_and_trade()

    assert len(broker.buy_orders) == Config.MAX_OPEN_POSITIONS, (
        f"opened {len(broker.buy_orders)} positions; expected the cap of "
        f"{Config.MAX_OPEN_POSITIONS} to be reached and then respected"
    )


# ----------------------------------------------------------------------
# Bug: SELL signals were generated with 0.75 confidence in a bear trend
# and then discarded -- the live loop only ever branched on BUY, so a
# position could only ever be closed by its own bracket.
# ----------------------------------------------------------------------
def test_sell_signal_closes_an_open_position(monkeypatch):
    """A SELL signal must actually close the position it refers to."""
    broker = FakeBroker()
    broker._positions["AAPL"] = {
        "symbol": "AAPL", "qty": 10, "entry_price": 150.0,
        "current_price": 150.0, "unrealized_pl": 0.0, "unrealized_plpc": 0.0,
    }
    signals = {"AAPL": _signal("SELL", "AAPL", confidence=0.75)}
    orch = _make_orchestrator(monkeypatch, broker, signals)

    orch.scan_and_trade()

    assert len(broker.sell_orders) == 1, "a SELL signal did not close the position"
    assert broker.sell_orders[0]["symbol"] == "AAPL"


def test_sell_signal_does_nothing_without_a_position(monkeypatch):
    """SELL on a symbol with no position must not place an order."""
    broker = FakeBroker()
    signals = {"AAPL": _signal("SELL", "AAPL", confidence=0.75)}
    orch = _make_orchestrator(monkeypatch, broker, signals)

    orch.scan_and_trade()

    assert broker.sell_orders == []
    assert broker.buy_orders == []


# ----------------------------------------------------------------------
# Bug: held positions were dropped from the price map when their data
# fetch degraded, so their stop loss was never re-checked.
# ----------------------------------------------------------------------
def test_held_position_is_still_monitored_when_its_data_degrades(monkeypatch):
    """A held symbol must stay in the price map even if its bars are thin."""
    broker = FakeBroker()
    broker._positions["AAPL"] = {
        "symbol": "AAPL", "qty": 10, "entry_price": 150.0,
        "current_price": 150.0, "unrealized_pl": 0.0, "unrealized_plpc": 0.0,
    }
    # AAPL returns only 10 bars -> the loop's `continue` guard would skip it.
    frames = {"AAPL": _bars(10)}
    signals = {"AAPL": _signal("HOLD", "AAPL")}
    orch = _make_orchestrator(monkeypatch, broker, signals, frames=frames)

    orch.scan_and_trade()

    assert broker.exits, "update_simulated_prices was never called"
    assert "AAPL" in broker.exits[0], (
        "a held position was dropped from the price map because its data fetch "
        "returned too few bars; its stop loss would never be checked again"
    )


# ----------------------------------------------------------------------
# Bug: calculate_trailing_stop was defined but never called, while the
# README advertised "manages trailing stops" as a live feature.
# ----------------------------------------------------------------------
def test_trailing_stop_is_applied_to_open_positions(monkeypatch):
    """The live loop must move stops on positions that have run up."""
    broker = FakeBroker()
    broker._positions["NVDA"] = {
        "symbol": "NVDA", "qty": 10, "entry_price": 100.0,
        "current_price": 120.0, "unrealized_pl": 200.0, "unrealized_plpc": 20.0,
        "stop_loss": 95.0,
    }
    signals = {"NVDA": _signal("HOLD", "NVDA")}
    orch = _make_orchestrator(monkeypatch, broker, signals, watchlist=["NVDA"])

    orch.scan_and_trade()

    assert broker.trailing_stop_updates, (
        "the trailing stop was never applied; risk/risk_manager.py defines "
        "calculate_trailing_stop but nothing calls it"
    )
    update = broker.trailing_stop_updates[0]
    assert update["symbol"] == "NVDA"
    assert update["new_stop"] > 95.0, "the trailing stop must only ever move up"


# ----------------------------------------------------------------------
# Bug: in simulator mode the fallback market clock returned next_open=None
# after the close, so the bot polled every 5 minutes all night and all
# weekend instead of sleeping.
# ----------------------------------------------------------------------
def test_fallback_clock_reports_next_open_after_the_close():
    """After the close the clock must report when the market next opens."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from execution.alpaca_broker import AlpacaBroker

    broker = AlpacaBroker(force_simulator=True)

    # 17:30 ET on a Wednesday -- after the 16:00 close.
    et_now = datetime(2026, 10, 21, 17, 30, tzinfo=ZoneInfo("America/New_York"))
    clock = broker._simulated_clock(et_now)

    assert clock["is_open"] is False
    assert clock["next_open"] is not None, (
        "next_open was None after the close; the bot would poll every 5 minutes "
        "all night instead of sleeping until the next session"
    )
    assert clock["next_open"] > et_now


def test_fallback_clock_reports_next_open_on_a_weekend():
    """On a Saturday the clock must point at Monday's open."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from execution.alpaca_broker import AlpacaBroker

    broker = AlpacaBroker(force_simulator=True)

    saturday = datetime(2026, 10, 24, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    clock = broker._simulated_clock(saturday)

    assert clock["is_open"] is False
    assert clock["next_open"] is not None
    assert clock["next_open"] > saturday
