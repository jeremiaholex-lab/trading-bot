"""Shared test fixtures.

The news/event-risk gate (risk/news_filter.py) fetches live feeds. The
suite is hermetic (no network), so the gate is disabled by default here;
tests that specifically exercise it re-enable it with monkeypatch.
"""
import pytest

from config import Config
from execution.trade_journal import TradeJournal
from safety.audit_ledger import AuditLedger


@pytest.fixture(autouse=True)
def _isolate_implicit_trade_journal(tmp_path, monkeypatch):
    original_init = TradeJournal.__init__

    def init_with_test_database(journal, db_path=None):
        test_db_path = db_path if db_path is not None else tmp_path / "trades.db"
        original_init(journal, db_path=test_db_path)

    monkeypatch.setattr(TradeJournal, "__init__", init_with_test_database)
    yield


@pytest.fixture(autouse=True)
def _isolate_implicit_audit_ledger(tmp_path, monkeypatch):
    """Keep tests from loading or modifying the developer's runtime ledger."""
    original_init = AuditLedger.__init__

    def init_with_test_ledger(ledger, ledger_dir=None, max_entries_per_file=10000):
        isolated_dir = ledger_dir if ledger_dir is not None else tmp_path / "audit_ledger"
        original_init(ledger, ledger_dir=isolated_dir, max_entries_per_file=max_entries_per_file)

    monkeypatch.setattr(AuditLedger, "__init__", init_with_test_ledger)
    yield


@pytest.fixture(autouse=True)
def _offline_news_gate(monkeypatch):
    """Keep the news gate from touching the network in every test.

    Production behaviour is unchanged; this only affects the test suite.
    Tests that want the gate's logic run restore it via
    monkeypatch.setattr(Config, "ENABLE_NEWS_FILTER", True).
    """
    monkeypatch.setattr(Config, "ENABLE_NEWS_FILTER", False)
    yield
