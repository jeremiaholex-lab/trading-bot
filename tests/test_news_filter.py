"""Hermetic tests for the breaking-news / event-risk gate (news_filter.py).

No network: the fetchers are stubbed and the pure decision logic
(NewsFilter._evaluate) is exercised directly.
"""
import datetime as dt

import pytest

from risk.news_filter import NewsFilter, NewsFetchError


NOW = dt.datetime(2026, 10, 28, 15, 0, tzinfo=dt.timezone.utc)


def _item(title, minutes_ago=5, important=False, form=None, items=None):
    when = NOW - dt.timedelta(minutes=minutes_ago)
    return {
        "id": "test:" + title, "ticker": "TSLA", "title": title,
        "summary": "", "link": "", "time": when, "important": important,
        "form": form, "items": items or [],
    }


def test_important_8k_blocks(monkeypatch):
    nf = NewsFilter()
    monkeypatch.setattr(nf, "_collect", lambda sym: [
        _item("SEC 8-K filed: earnings results", important=True, form="8-K",
              items=["2.02"]),
    ])
    blocked, reason = nf.is_blocked("TSLA", hours=24, now=NOW)
    assert blocked is True
    assert "Important SEC filing" in reason


def test_risk_headline_blocks(monkeypatch):
    nf = NewsFilter()
    monkeypatch.setattr(nf, "_collect", lambda sym: [
        _item("TSLA shares halt after probe opened"),
    ])
    blocked, reason = nf.is_blocked("TSLA", hours=24, now=NOW)
    assert blocked is True
    assert "News risk" in reason


def test_market_wide_headline_does_not_block(monkeypatch):
    """A headline about something else must not gate the symbol.

    Yahoo's feed carries market-wide news; a record exchange hack appears in
    every ticker's feed but is not about SPY. Without this guard the gate
    would halt all trading on unrelated headlines.
    """
    nf = NewsFilter()
    monkeypatch.setattr(nf, "_collect", lambda sym: [
        _item("Coinbase tumbles 11% as record Binance hack hits crypto"),
    ])
    blocked, reason = nf.is_blocked("SPY", hours=24, now=NOW)
    assert blocked is False
    assert reason == ""


def test_innocuous_headline_allows_by_default(monkeypatch):
    # important_only=True: a benign headline must NOT block.
    nf = NewsFilter()
    monkeypatch.setattr(nf, "_collect", lambda sym: [
        _item("Tesla announces new software update"),
    ])
    blocked, reason = nf.is_blocked("TSLA", hours=24, now=NOW)
    assert blocked is False
    assert reason == ""


def test_any_headline_blocks_when_not_important_only(monkeypatch):
    nf = NewsFilter()
    monkeypatch.setattr(nf, "_collect", lambda sym: [
        _item("Tesla announces new software update"),
    ])
    blocked, _ = nf.is_blocked("TSLA", hours=24, important_only=False, now=NOW)
    assert blocked is True


def test_item_outside_window_allows(monkeypatch):
    # 48 hours ago is outside a 24h window.
    nf = NewsFilter()
    monkeypatch.setattr(nf, "_collect", lambda sym: [
        _item("SEC 8-K filed: earnings results", minutes_ago=48 * 60,
              important=True, items=["2.02"]),
    ])
    blocked, _ = nf.is_blocked("TSLA", hours=24, now=NOW)
    assert blocked is False


def test_fetch_failure_allows_and_logs(monkeypatch):
    # A network outage must never halt trading on a false positive.
    nf = NewsFilter()

    def boom(sym):
        raise NewsFetchError("could not reach host")

    monkeypatch.setattr(nf, "_collect", boom)
    blocked, reason = nf.is_blocked("TSLA", hours=24, now=NOW)
    assert blocked is False
    assert reason == ""


def test_cache_short_circuits_fetcher(monkeypatch):
    calls = []

    def collect(sym):
        calls.append(sym)
        return [_item("Tesla shares halt after probe opened")]

    nf = NewsFilter()
    monkeypatch.setattr(nf, "_collect", collect)
    nf.is_blocked("TSLA", hours=24, now=NOW)
    nf.is_blocked("TSLA", hours=24, now=NOW + dt.timedelta(minutes=1))
    # Second call within TTL must reuse the cache, not re-fetch.
    assert len(calls) == 1


def test_evaluate_blocks_important_only_false_without_risk():
    # important_only=False and a benign item still blocks.
    nf = NewsFilter()
    items = [_item("Company announces routine update")]
    blocked, _ = nf._evaluate(items, NOW, 24.0, False)
    assert blocked is True


def test_evaluate_empty_items_allows():
    nf = NewsFilter()
    blocked, reason = nf._evaluate([], NOW, 24.0, True)
    assert blocked is False
    assert reason == ""
