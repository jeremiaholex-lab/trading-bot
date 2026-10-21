"""Regression tests for safety-critical defects.

Each test in this module corresponds to a bug that could cause real financial
loss, silently disable trading, or leak credentials. They are deliberately
written against *observable behaviour* rather than implementation details, so
they keep working as the code is refactored.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

from config import Config

ROOT = Path(__file__).resolve().parent.parent


def _collect_suite():
    """Run pytest collection over the repo and return the combined output.

    `-s` is essential: without it pytest captures stdout, so module-level
    side effects in an imported script are hidden and the test would pass
    vacuously.
    """
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-s", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


# ----------------------------------------------------------------------
# Bug: running the test suite placed live Alpaca orders.
#
# `test_scan.py` lived at the repo root with module-level code that built a
# TradingBotOrchestrator and called scan_and_trade(). Pytest's default
# `test_*.py` glob imported it on every collection -- including
# `pytest --collect-only` -- so every test run connected to Alpaca and
# submitted bracket orders.
# ----------------------------------------------------------------------
def test_collecting_tests_never_executes_trading_code():
    """Importing the test suite must not run the bot or place orders."""
    proc = _collect_suite()
    combined = proc.stdout + proc.stderr

    assert "Scanning Watchlist" not in combined, (
        "Collecting the test suite executed the live trading loop "
        "(test_scan.py is being imported at collection time)."
    )
    assert "EXECUTES BUY" not in combined, (
        "Collecting the test suite executed live order placement."
    )
    assert "Creating bot" not in combined, (
        "Collecting the test suite imported the root-level test_scan.py script."
    )


def test_root_level_test_script_is_import_safe():
    """A root-level script must not run the bot when it is merely imported."""
    proc = subprocess.run(
        [sys.executable, "-c", "import test_scan"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    combined = proc.stdout + proc.stderr

    assert "Scanning Watchlist" not in combined, (
        "Importing test_scan.py executes the trading loop at module level. "
        "Guard it with `if __name__ == '__main__':`."
    )


def test_pytest_is_scoped_to_the_tests_directory():
    """pytest must be configured to collect from tests/ only."""
    ini = ROOT / "pytest.ini"
    assert ini.exists(), "pytest.ini is missing; bare `pytest` can collect root-level scripts."
    content = ini.read_text(encoding="utf-8")
    assert "testpaths" in content
    assert "tests" in content


# ----------------------------------------------------------------------
# Bug: invalid Alpaca credentials silently downgraded to the local
# simulator. `alpaca_broker.py` caught the auth failure, logged a warning
# and carried on trading nothing while the dashboard claimed it was live.
# ----------------------------------------------------------------------
class _FakeTradingClient:
    """Stands in for alpaca.trading.client.TradingClient with a bad token."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def get_account(self):
        raise RuntimeError('{"message": "unauthorized."}')


def test_invalid_credentials_raise_instead_of_silently_simulating(monkeypatch):
    """Credentials that are present but rejected must stop the bot, not downgrade it."""
    from execution.alpaca_broker import AlpacaBroker, BrokerAuthenticationError

    monkeypatch.setattr(Config, "ALPACA_API_KEY", "PKinvalidkeyinvalidkey12")
    monkeypatch.setattr(Config, "ALPACA_SECRET_KEY", "invalidsecret")
    monkeypatch.setattr(Config, "ALLOW_SIMULATOR_FALLBACK", False)
    monkeypatch.setattr("alpaca.trading.client.TradingClient", _FakeTradingClient)

    with pytest.raises(BrokerAuthenticationError):
        AlpacaBroker()


def test_invalid_credentials_message_names_the_cause(monkeypatch):
    """The raised error must tell the operator what went wrong and how to fix it."""
    from execution.alpaca_broker import AlpacaBroker, BrokerAuthenticationError

    monkeypatch.setattr(Config, "ALPACA_API_KEY", "PKinvalidkeyinvalidkey12")
    monkeypatch.setattr(Config, "ALPACA_SECRET_KEY", "invalidsecret")
    monkeypatch.setattr(Config, "ALLOW_SIMULATOR_FALLBACK", False)
    monkeypatch.setattr("alpaca.trading.client.TradingClient", _FakeTradingClient)

    with pytest.raises(BrokerAuthenticationError) as excinfo:
        AlpacaBroker()

    message = str(excinfo.value)
    assert "unauthorized" in message.lower()
    assert "ALLOW_SIMULATOR_FALLBACK" in message


def test_missing_credentials_still_use_the_zero_config_simulator(monkeypatch):
    """With no keys at all the safe simulator remains the default path."""
    from execution.alpaca_broker import AlpacaBroker

    monkeypatch.setattr(Config, "ALPACA_API_KEY", "")
    monkeypatch.setattr(Config, "ALPACA_SECRET_KEY", "")

    broker = AlpacaBroker()
    assert broker.is_simulated is True


def test_simulator_fallback_can_be_opted_into_explicitly(monkeypatch):
    """Operators can deliberately run on the simulator even with bad keys."""
    from execution.alpaca_broker import AlpacaBroker

    monkeypatch.setattr(Config, "ALPACA_API_KEY", "PKinvalidkeyinvalidkey12")
    monkeypatch.setattr(Config, "ALPACA_SECRET_KEY", "invalidsecret")
    monkeypatch.setattr(Config, "ALLOW_SIMULATOR_FALLBACK", True)
    monkeypatch.setattr("alpaca.trading.client.TradingClient", _FakeTradingClient)

    broker = AlpacaBroker()
    assert broker.is_simulated is True


def test_force_simulator_never_touches_the_network(monkeypatch):
    """force_simulator=True must not construct a broker client at all."""
    from execution.alpaca_broker import AlpacaBroker

    def explode(**kwargs):
        raise AssertionError("force_simulator must not construct an Alpaca client")

    monkeypatch.setattr("alpaca.trading.client.TradingClient", explode)

    broker = AlpacaBroker(force_simulator=True)
    assert broker.is_simulated is True


# ----------------------------------------------------------------------
# Bug: the Alpaca account number was sitting in four untracked output files
# that .gitignore did not cover, so `git add .` would publish it.
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "filename",
    [
        "scan_output.txt",
        "scan_debug.txt",
        "scan_full.log",
        "full_output.txt",
        "logs/bot.log",
    ],
)
def test_runtime_outputs_are_gitignored(filename):
    """Generated output files must never be committable."""
    proc = subprocess.run(
        ["git", "check-ignore", "-q", filename],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"{filename} is not covered by .gitignore"


# A name-denylist is the wrong shape for this hazard: the leak that actually
# happened used filenames nobody had listed yet (full_out.txt, ws_out.txt).
# These tests assert the property that matters instead -- no untracked file
# may contain the account number -- so they hold for files that do not exist
# yet.
ACCOUNT_NUMBER_SHAPE = re.compile(r"\bPA[0-9A-Z]{8,}\b")


def _untracked_text_files():
    """Untracked files git would actually add, as paths."""
    proc = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip("not a git work tree; the leak check needs git")
    return [p for p in proc.stdout.split("\0") if p]


def test_no_untracked_file_contains_an_account_number():
    """No file that `git add .` would publish may contain an Alpaca account number.

    This catches the failure the filename list missed: a new output capture
    with a name .gitignore has never heard of.
    """
    offenders = []
    for relpath in _untracked_text_files():
        path = ROOT / relpath
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        match = ACCOUNT_NUMBER_SHAPE.search(text)
        if match:
            offenders.append(f"{relpath} (contains {match.group(0)[:4]}...)")

    assert not offenders, (
        "Untracked files would publish an Alpaca account number on "
        "`git add .`: " + ", ".join(offenders)
    )
