"""Shared test fixtures.

The news/event-risk gate (risk/news_filter.py) fetches live feeds. The
suite is hermetic (no network), so the gate is disabled by default here;
tests that specifically exercise it re-enable it with monkeypatch.
"""
import pytest

from config import Config


@pytest.fixture(autouse=True)
def _offline_news_gate(monkeypatch):
    """Keep the news gate from touching the network in every test.

    Production behaviour is unchanged; this only affects the test suite.
    Tests that want the gate's logic run restore it via
    monkeypatch.setattr(Config, "ENABLE_NEWS_FILTER", True).
    """
    monkeypatch.setattr(Config, "ENABLE_NEWS_FILTER", False)
    yield
