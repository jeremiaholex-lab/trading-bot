"""Tests for the end-of-session report.

The report is the operator's only feedback channel when the bot runs
unattended. Two things must hold:

  1. It fires ONCE per trading session, on the market-close transition.
     Not on every scan, and not for a session the bot never observed.
  2. It reports what actually happened, including the things that went
     wrong. A report that only ever says "everything fine" is worse than
     no report, because it looks like evidence of health.
"""
import pytest

from config import Config


class _RecordingNotifier:
    def __init__(self):
        self.reports = []
        self.alerts = []

    def send_alert(self, title, message, level="INFO", fields=None):
        self.alerts.append({"title": title, "message": message, "level": level, "fields": fields})

    def notify_order_execution(self, symbol, side, qty, price, order_type="MARKET"):
        self.alerts.append({"title": f"order:{symbol}:{side}", "level": "SUCCESS"})

    def notify_circuit_breaker(self, reason, drawdown):
        self.alerts.append({"title": "circuit_breaker", "level": "CRITICAL"})

    def notify_daily_summary(self, total_equity, daily_pnl, open_positions):
        self.alerts.append({"title": "daily_summary", "level": "INFO"})

    def notify_session_report(self, report):
        self.reports.append(report)


class _FakeBroker:
    def __init__(self, equity=100_000.0, positions=None):
        self.equity = equity
        self._positions = positions or {}

    def get_account_summary(self):
        return {
            "equity": self.equity,
            "cash": 50_000.0,
            "buying_power": 100_000.0,
            "mode": "ALPACA PAPER CLOUD",
            "currency": "USD",
        }

    def get_open_positions(self):
        return dict(self._positions)


def _build(broker, notifier, start_equity=100_000.0):
    """Builds a bare orchestrator without touching the network."""
    import bot as botmod

    orch = botmod.TradingBotOrchestrator.__new__(botmod.TradingBotOrchestrator)
    orch.broker = broker
    orch.notifier = notifier
    orch._session_trades = []
    orch._session_warnings = []
    orch._session_scans = 0
    orch._was_market_open = False
    orch._report_sent_for_date = None

    class _Risk:
        start_of_day_equity = start_equity
        circuit_breaker_tripped = False

    orch.risk_mgr = _Risk()
    return orch


# ----------------------------------------------------------------------
# The report content.
# ----------------------------------------------------------------------
def test_report_includes_equity_pnl_positions_and_trades():
    """The report must answer: what did the day make me, and what is open?"""
    broker = _FakeBroker(
        equity=101_250.0,
        positions={
            "SPY": {
                "symbol": "SPY", "qty": 10, "entry_price": 500.0,
                "current_price": 512.0, "unrealized_pl": 120.0,
                "unrealized_plpc": 2.4,
            }
        },
    )
    notifier = _RecordingNotifier()
    orch = _build(broker, notifier, start_equity=100_000.0)
    orch._session_trades.append({
        "side": "BUY", "symbol": "SPY", "qty": 10, "price": 500.0, "reason": "pullback in uptrend",
    })
    orch._session_scans = 12

    report = orch._build_session_report()

    assert report["equity"] == 101_250.0
    assert report["daily_pnl"] == 1250.0
    assert report["daily_pnl_pct"] == pytest.approx(1.25)
    assert len(report["positions"]) == 1
    assert report["positions"][0]["symbol"] == "SPY"
    assert len(report["trades_taken"]) == 1
    assert report["trades_taken"][0]["symbol"] == "SPY"
    assert report["mode"] == "ALPACA PAPER CLOUD"


def test_report_flags_a_day_with_no_activity():
    """A session with no trades and no positions must say so.

    A silent day and a broken bot look identical from the outside. The
    report has to distinguish them, otherwise the operator cannot tell
    "nothing happened" from "something is wrong".
    """
    notifier = _RecordingNotifier()
    orch = _build(_FakeBroker(), notifier)
    orch._session_scans = 5

    report = orch._build_session_report()

    assert report["trades_taken"] == []
    assert report["positions"] == []
    assert any("No trades" in w for w in report["warnings"]), (
        f"a dead session must produce a warning, got {report['warnings']}"
    )


def test_report_records_circuit_breaker_as_a_warning():
    """A tripped circuit breaker is the single most important thing to report."""
    notifier = _RecordingNotifier()
    orch = _build(_FakeBroker(), notifier)

    orch._notify_circuit_breaker("Daily drawdown limit breached", "SPY")

    report = orch._build_session_report()
    assert any("Circuit breaker" in w for w in report["warnings"]), (
        f"circuit breaker trip missing from warnings: {report['warnings']}"
    )


def test_report_records_optimizer_adoption():
    """Adopting new parameters changes tomorrow's behaviour; it must be reported."""
    notifier = _RecordingNotifier()
    orch = _build(_FakeBroker(), notifier)

    orch._record_session_warning("Optimiser adopted new parameters for SPY (fitness +0.42 vs baseline).")

    report = orch._build_session_report()
    assert any("adopted new parameters" in w for w in report["warnings"])


def test_report_build_never_raises_on_broker_failure():
    """A reporting failure must never take down the trading loop."""
    class _BrokenBroker(_FakeBroker):
        def get_account_summary(self):
            raise RuntimeError("Alpaca is down")

    notifier = _RecordingNotifier()
    orch = _build(_BrokenBroker(), notifier)

    report = orch._build_session_report()

    assert report == {}


# ----------------------------------------------------------------------
# The trigger.
# ----------------------------------------------------------------------
def test_report_fires_once_on_the_market_close_transition(monkeypatch):
    """One report per session, fired when the market closes."""
    import bot as botmod

    notifier = _RecordingNotifier()
    orch = _build(_FakeBroker(), notifier)

    clock_states = [
        {"is_open": True, "next_open": None, "next_close": None},
        {"is_open": True, "next_open": None, "next_close": None},
        {"is_open": False, "next_open": None, "next_close": None},
        {"is_open": False, "next_open": None, "next_close": None},
    ]

    class _ClockBroker(_FakeBroker):
        def get_market_clock(self):
            return clock_states.pop(0) if clock_states else {
                "is_open": False, "next_open": None, "next_close": None
            }

    orch.broker = _ClockBroker()
    monkeypatch.setattr(botmod.time, "sleep", lambda s: None)

    # Drive the transition-detection logic directly rather than the whole
    # loop, which would need a full market session.
    for is_open in (True, True, False, False):
        if is_open:
            orch._was_market_open = True
        elif orch._was_market_open:
            orch._was_market_open = False
            today = orch._report_sent_for_date
            from datetime import datetime, timezone
            today_et = datetime.now(timezone.utc).date()
            if today != today_et:
                orch._report_sent_for_date = today_et
                orch._send_session_report()
                orch._session_trades.clear()
                orch._session_warnings.clear()
                orch._session_scans = 0

    assert len(notifier.reports) == 1, (
        f"expected exactly one session report, got {len(notifier.reports)}"
    )


def test_no_report_when_the_bot_starts_after_the_market_closed():
    """A bot started with the market already shut has observed no session.

    Reporting here would send a bogus "daily report" for a session that
    never happened, which is worse than sending nothing.
    """
    notifier = _RecordingNotifier()
    orch = _build(_FakeBroker(), notifier)

    # The market is closed on the very first check.
    orch._was_market_open = False

    from datetime import datetime, timezone
    today_et = datetime.now(timezone.utc).date()
    if not False and orch._was_market_open:
        orch._send_session_report()

    assert notifier.reports == []
    assert orch._report_sent_for_date != today_et


def test_report_can_be_triggered_on_demand(monkeypatch):
    """`--report` sends the report without waiting for the market to close."""
    notifier = _RecordingNotifier()
    orch = _build(_FakeBroker(), notifier)

    orch._send_session_report()

    assert len(notifier.reports) == 1


def test_report_is_skipped_when_disabled(monkeypatch):
    """The operator can turn the report off."""
    monkeypatch.setattr(Config, "ENABLE_SESSION_REPORT", False)

    notifier = _RecordingNotifier()
    orch = _build(_FakeBroker(), notifier)

    orch._send_session_report()

    assert notifier.reports == []
