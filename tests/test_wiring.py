"""Regression tests for wiring the conviction scanner and notifier into the loop.

strategy/scanner.py and execution/webhook.py were fully implemented, tested,
and imported by nothing but the test suite. They are the two pieces that
would make the bot *selective* (rank candidates by conviction rather than
taking every signal) and *observable* (tell the operator what it did).
"""
import numpy as np
import pandas as pd
import pytest

from config import Config


class RecordingNotifier:
    """Stands in for execution.webhook.WebhookNotifier."""

    def __init__(self):
        self.alerts = []

    def send_alert(self, title, message, level="INFO", fields=None):
        self.alerts.append({"title": title, "message": message, "level": level, "fields": fields})

    def notify_order_execution(self, symbol, side, qty, price, order_type="MARKET"):
        self.alerts.append({"title": f"order:{symbol}:{side}", "level": "SUCCESS"})

    def notify_circuit_breaker(self, reason, drawdown):
        self.alerts.append({"title": "circuit_breaker", "level": "CRITICAL"})

    def notify_daily_summary(self, total_equity, daily_pnl, open_positions):
        self.alerts.append({"title": "daily_summary", "level": "INFO"})


def _bars(n=120, price=150.0, seed=0):
    rng = np.random.default_rng(seed)
    close = price + np.cumsum(rng.normal(0.02, 0.3, n))
    return pd.DataFrame(
        {
            "Open": close, "High": close + 0.4, "Low": close - 0.4,
            "Close": close, "Volume": rng.integers(5000, 20000, n),
        },
        index=pd.date_range("2026-01-01", periods=n, freq="15min"),
    )


def _build(monkeypatch, broker, signals, watchlist, frames=None, notifier=None):
    import bot as botmod
    from strategy.adaptive_strategy import AdaptiveStrategy

    monkeypatch.setattr(botmod.Config, "WATCHLIST", watchlist)

    class ScriptedStrategy:
        def __init__(self, params=None):
            self.params = params

        def generate_signal(self, symbol, df):
            return signals[symbol] if symbol in signals else _signal("HOLD", symbol)

    monkeypatch.setattr(botmod, "AdaptiveStrategy", ScriptedStrategy)
    monkeypatch.setattr(botmod, "AssetScanner", lambda wl: _FakeScanner(frames or {}), raising=False)
    if notifier is not None:
        monkeypatch.setattr(botmod, "WebhookNotifier", lambda: notifier, raising=False)

    orch = botmod.TradingBotOrchestrator.__new__(botmod.TradingBotOrchestrator)
    orch.market_data = _FakeData(frames or {})
    orch.broker = broker
    orch.risk_mgr = _FakeRisk()
    orch.self_improver = type("S", (), {"get_symbol_params": staticmethod(lambda s: None), "state": {}})()
    orch.notifier = notifier
    return orch


class _FakeData:
    def __init__(self, frames):
        self.frames = frames

    def get_historical_bars(self, symbol, timeframe="15Min", days_back=60):
        return self.frames.get(symbol, _bars())


class _FakeScanner:
    def __init__(self, frames):
        self.frames = frames

    def scan_and_rank(self, market_data_map):
        # Rank by a stable synthetic score so the ordering is predictable.
        ranked = []
        for i, sym in enumerate(sorted(market_data_map)):
            ranked.append({
                "symbol": sym,
                "score": float(len(market_data_map) - i),
                "regime": "TRENDING_BULL",
                "reasons": "test",
            })
        ranked.sort(key=lambda x: x["score"], reverse=True)
        return ranked


class _FakeRisk:
    def update_start_of_day_equity(self, equity):
        pass

    def calculate_position_size(self, signal, portfolio_equity, cash_available,
                               current_open_positions_count, already_holding_symbol):
        class R:
            allowed = True
            shares = 10
            cost = 1500.0
            risk_amount = 50.0
            rejection_reason = ""
        return R()

    def calculate_trailing_stop(self, entry_price, current_price, current_stop, atr, atr_multiplier):
        return current_stop

    def check_circuit_breaker(self, equity):
        return False


def _signal(action, symbol, confidence=0.9, price=150.0):
    from strategy.adaptive_strategy import TradeSignal
    return TradeSignal(
        action=action, symbol=symbol, entry_price=price,
        stop_loss_price=price - 5.0, take_profit_price=price + 10.0,
        confidence=confidence, regime="TRENDING_BULL", reason="test", metadata={},
    )


class _Broker:
    def __init__(self):
        self.buy_orders = []
        self.sell_orders = []
        self.positions = {}

    def get_account_summary(self):
        return {"equity": 100000.0, "cash": 100000.0, "buying_power": 200000.0, "mode": "TEST"}

    def get_open_positions(self):
        return dict(self.positions)

    def place_bracket_buy_order(self, symbol, qty, entry_price, stop_loss_price, take_profit_price):
        self.buy_orders.append(symbol)
        self.positions[symbol] = {
            "symbol": symbol, "qty": qty, "entry_price": entry_price,
            "current_price": entry_price, "unrealized_pl": 0.0, "unrealized_plpc": 0.0,
            "stop_loss": stop_loss_price,
        }
        class R:
            success = True
            message = "ok"
        return R()

    def place_close_position_order(self, symbol, qty, reason=""):
        self.sell_orders.append(symbol)
        self.positions.pop(symbol, None)
        class R:
            success = True
            message = "ok"
        return R()

    def update_simulated_prices(self, price_map):
        return []

    def update_trailing_stop(self, symbol, new_stop):
        return True


# ----------------------------------------------------------------------
# Bug: strategy/scanner.py was implemented and tested, but bot.py never
# imported it. The bot took every BUY signal it saw, so a weak setup could
# consume the last position slot ahead of a high-conviction one.
# ----------------------------------------------------------------------
def test_scanner_is_used_to_prioritise_candidates(monkeypatch):
    """The conviction scanner must be consulted during a scan."""
    import bot as botmod

    broker = _Broker()
    watchlist = ["AAA", "BBB", "CCC"]
    signals = {sym: _signal("BUY", sym) for sym in watchlist}
    orch = _build(monkeypatch, broker, signals, watchlist)

    called = {"n": 0}
    original_init = botmod.TradingBotOrchestrator.__init__ if hasattr(botmod.TradingBotOrchestrator, "__init__") else None

    # Spy on the scanner by replacing the class attribute usage.
    class SpyScanner(_FakeScanner):
        def scan_and_rank(self, market_data_map):
            called["n"] += 1
            return super().scan_and_rank(market_data_map)

    monkeypatch.setattr(botmod, "AssetScanner", lambda wl: SpyScanner({}))

    orch.scan_and_trade()

    assert called["n"] > 0, (
        "the conviction scanner was never used; bot.py does not import "
        "strategy/scanner.py"
    )


# ----------------------------------------------------------------------
# Bug: execution/webhook.py was implemented and tested, but bot.py never
# imported it, so the operator received no notification of trades or
# circuit-breaker trips.
# ----------------------------------------------------------------------
def test_notifier_receives_order_execution_alerts(monkeypatch):
    """A filled order must produce a notification."""
    broker = _Broker()
    notifier = RecordingNotifier()
    watchlist = ["AAA"]
    signals = {"AAA": _signal("BUY", "AAA")}
    orch = _build(monkeypatch, broker, signals, watchlist, notifier=notifier)

    orch.scan_and_trade()

    titles = [a["title"] for a in notifier.alerts]
    assert any("AAA" in t for t in titles), (
        f"no notification was raised for a filled order; alerts were {titles}"
    )


def test_notifier_receives_circuit_breaker_alerts(monkeypatch):
    """A tripped circuit breaker must produce a critical notification."""
    broker = _Broker()
    notifier = RecordingNotifier()
    watchlist = ["AAA"]
    signals = {"AAA": _signal("BUY", "AAA")}
    orch = _build(monkeypatch, broker, signals, watchlist, notifier=notifier)

    # Force the risk manager to reject the trade the way a tripped breaker does.
    class BreakerRisk(_FakeRisk):
        def calculate_position_size(self, *a, **kw):
            class R:
                allowed = False
                shares = 0
                cost = 0.0
                risk_amount = 0.0
                rejection_reason = "Daily drawdown circuit breaker active."
            return R()

    orch.risk_mgr = BreakerRisk()

    orch.scan_and_trade()

    titles = [a["title"] for a in notifier.alerts]
    assert any("circuit" in t.lower() for t in titles), (
        f"no circuit-breaker notification was raised; alerts were {titles}"
    )


# ----------------------------------------------------------------------
# The unused `schedule` dependency was never imported anywhere.
# ----------------------------------------------------------------------
def test_schedule_dependency_is_not_required():
    """requirements.txt must not carry dependencies the code never uses."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")

    source = ""
    for path in root.rglob("*.py"):
        if "venv" in path.parts or "tests" in path.parts:
            continue
        source += path.read_text(encoding="utf-8")

    # Distribution names do not always match the import name.
    import_names = {
        "alpaca-py": "alpaca",
        "python-dotenv": "dotenv",
        "pyyaml": "yaml",
        "scikit-learn": "sklearn",
    }

    for line in requirements.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        package = line.split(">=")[0].split("==")[0].split(">")[0].strip()
        if not package:
            continue
        import_name = import_names.get(package, package)
        if import_name not in source:
            pytest.fail(
                f"requirements.txt lists {package!r} but no source file imports {import_name!r}"
            )
