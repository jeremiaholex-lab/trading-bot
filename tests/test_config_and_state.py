"""Regression tests for configuration coherence and simulator durability."""
import json
from pathlib import Path

import pytest

from config import Config


# ----------------------------------------------------------------------
# Bug: Config.validate() accepted BAR_TIMEFRAME values ("1m", "5m",
# "1h") that appeared in neither the Alpaca unit map nor the yfinance
# interval map. Both maps use .get(timeframe, <15-minute default>), so the
# bar size was silently downgraded to 15 minutes while the operator
# believed they were trading hourly.
# ----------------------------------------------------------------------
def test_every_valid_timeframe_is_handled_by_the_data_layer():
    """Config and the data layer must agree on the set of timeframes."""
    import inspect
    from data.market_data import MarketDataProvider

    market_data_file = Path(inspect.getfile(MarketDataProvider)).resolve()
    source = market_data_file.read_text(encoding="utf-8")

    for tf in Config.VALID_TIMEFRAMES:
        assert f'"{tf}"' in source, (
            f"timeframe {tf!r} is accepted by Config.validate() but is not "
            f"handled by the data layer; it would be silently downgraded to "
            f"15 minutes."
        )


def test_config_rejects_an_unhandled_timeframe(monkeypatch):
    """A timeframe the data layer cannot serve must fail validation."""
    monkeypatch.setattr(Config, "BAR_TIMEFRAME", "7Min")
    errors = Config.validate()
    assert any("BAR_TIMEFRAME" in e for e in errors), (
        "an unsupported timeframe was accepted; it would be silently "
        "downgraded to 15 minutes at runtime"
    )


def test_config_accepts_the_documented_timeframes(monkeypatch):
    """The timeframes the README documents must all validate."""
    for tf in ["1Min", "5Min", "15Min", "1Hour", "1Day"]:
        monkeypatch.setattr(Config, "BAR_TIMEFRAME", tf)
        errors = [e for e in Config.validate() if "BAR_TIMEFRAME" in e]
        assert not errors, f"{tf} was rejected: {errors}"


# ----------------------------------------------------------------------
# Bug: the local simulator kept cash and positions in memory only, so
# every restart reset the account to $100,000 with no positions -- while
# the bot kept its tuned strategy parameters.
# ----------------------------------------------------------------------
def test_simulator_state_survives_a_restart(tmp_path):
    """Positions and cash must be restored when the bot restarts."""
    from execution.alpaca_broker import AlpacaBroker

    state_file = tmp_path / "sim_state.json"

    broker = AlpacaBroker(force_simulator=True, state_file=state_file)
    broker.place_bracket_buy_order(
        symbol="SPY", qty=10, entry_price=500.0,
        stop_loss_price=490.0, take_profit_price=520.0,
    )
    cash_after_buy = broker.sim_cash
    assert state_file.exists(), "the simulator never persisted its state"

    # A fresh broker, as if the process had been restarted.
    restarted = AlpacaBroker(force_simulator=True, state_file=state_file)
    assert restarted.sim_cash == cash_after_buy
    assert "SPY" in restarted.sim_positions
    assert restarted.sim_positions["SPY"].qty == 10


def test_simulator_state_file_contains_no_credentials(tmp_path):
    """The persisted file must hold portfolio state only."""
    from execution.alpaca_broker import AlpacaBroker

    state_file = tmp_path / "sim_state.json"
    broker = AlpacaBroker(force_simulator=True, state_file=state_file)
    broker.place_bracket_buy_order(
        symbol="SPY", qty=1, entry_price=100.0,
        stop_loss_price=95.0, take_profit_price=110.0,
    )

    content = state_file.read_text(encoding="utf-8")
    assert Config.ALPACA_API_KEY not in content or not Config.ALPACA_API_KEY
    assert "api_key" not in content.lower()


def test_simulator_corrupt_state_falls_back_safely(tmp_path):
    """A corrupt state file must not stop the bot from starting."""
    from execution.alpaca_broker import AlpacaBroker

    state_file = tmp_path / "sim_state.json"
    state_file.write_text("{ not json at all", encoding="utf-8")

    broker = AlpacaBroker(force_simulator=True, state_file=state_file)
    assert broker.sim_cash == 100000.0
    assert broker.sim_positions == {}


# ----------------------------------------------------------------------
# Bug: the dashboard labelled datetime.now() as "UTC" when it is local
# time, and a configuration error inside the daemon thread killed the
# trading loop silently while the dashboard kept showing $0.00.
# ----------------------------------------------------------------------
def test_dashboard_timestamp_is_actually_utc():
    """The dashboard must show UTC, and label it as UTC."""
    from datetime import datetime, timezone

    import app as appmod

    now_utc = datetime.now(timezone.utc)
    rendered = appmod._utc_timestamp(now_utc)

    assert "UTC" in rendered
    assert rendered.endswith("UTC")
    # The rendered time must match the UTC time, not the local one.
    assert now_utc.strftime("%Y-%m-%d") in rendered


def test_dashboard_reports_a_dead_trading_loop():
    """If the trading thread died, the dashboard must say so."""
    import app as appmod

    status = appmod._bot_status(orchestrator=None, thread=None)
    assert status["healthy"] is False
    assert status["message"]


def test_dashboard_reports_a_running_trading_loop():
    """A live orchestrator with a live thread is healthy."""
    import app as appmod

    class FakeOrch:
        broker = None

    class FakeThread:
        def is_alive(self):
            return True

    status = appmod._bot_status(orchestrator=FakeOrch(), thread=FakeThread())
    assert status["healthy"] is True


# ----------------------------------------------------------------------
# Bug: config.validate() rejected any ticker that was not purely
# alphanumeric, which rules out class shares like BRK.B and BF.B that
# trade on the NYSE.
# ----------------------------------------------------------------------
def test_dotted_class_share_tickers_are_valid(monkeypatch):
    """BRK.B is a real, liquid ticker and must be accepted."""
    monkeypatch.setattr(Config, "WATCHLIST", ["SPY", "BRK.B", "BF.B"])
    errors = [e for e in Config.validate() if "WATCHLIST" in e]
    assert not errors, f"dotted class-share tickers were rejected: {errors}"


def test_bogus_tickers_are_still_rejected(monkeypatch):
    """Genuinely malformed tickers must still fail validation."""
    monkeypatch.setattr(Config, "WATCHLIST", ["not a ticker!", "$$$"])
    errors = [e for e in Config.validate() if "WATCHLIST" in e]
    assert errors, "malformed tickers were accepted"
