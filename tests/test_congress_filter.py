"""Hermetic tests for the congressional disclosure watcher (congress_filter.py).

Parsing is exercised offline; the network index fetch is stubbed.
"""
import datetime as dt

import pytest

from risk.congress_filter import CongressFilter, CongressError


def _index_xml():
    """A minimal House Clerk index XML with two PTR filings."""
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<FinancialDisclosure>
  <Member>
    <FilingType>P</FilingType>
    <FilingDate>10/15/2026</FilingDate>
    <DocID>20035143</DocID>
    <First>Nancy</First><Last>Pelosi</Last>
    <StateDst>CA-11</StateDst><Year>2026</Year>
  </Member>
  <Member>
    <FilingType>P</FilingType>
    <FilingDate>10/20/2026</FilingDate>
    <DocID>20035144</DocID>
    <First>Someone</First><Last>Else</Last>
    <StateDst>TX-01</StateDst><Year>2026</Year>
  </Member>
</FinancialDisclosure>"""


def test_parse_index_returns_ptr_rows():
    rows = CongressFilter._parse_index(_index_xml(), 2026)
    assert len(rows) == 2
    ptrs = [r for r in rows if r["type"] == "P"]
    assert len(ptrs) == 2
    assert ptrs[0]["filed"] == "2026-10-15"
    assert ptrs[0]["doc_id"] == "20035143"
    assert ptrs[0]["name"] == "Nancy Pelosi"


def test_recent_trades_filters_by_date_and_symbol(monkeypatch):
    cf = CongressFilter()
    # Stub the index load so no network is touched.
    monkeypatch.setattr(cf, "_load_index", lambda year: CongressFilter._parse_index(
        _index_xml(), year))
    # Stub PDF reading: first PTR contains TSLA, second contains AAPL.
    monkeypatch.setattr(cf, "_read_ptrs", lambda rows, year: [
        {"doc_id": r["doc_id"], "member": r["name"], "district": r["district"],
         "filed": r["filed"], "url": r["url"],
         "tickers": ["TSLA"] if r["doc_id"] == "20035143" else ["AAPL"]}
        for r in rows
    ])

    matches = cf.recent_trades(
        ["TSLA"], since_days=45, now=dt.date(2026, 10, 30)
    )
    assert len(matches) == 1
    assert matches[0]["matched_tickers"] == ["TSLA"]
    assert matches[0]["member"] == "Nancy Pelosi"


def test_extract_tickers_from_ptr_text():
    text = (
        "1  SP  Tesla, Inc. (TSLA)  P 08/21/2026 08/25/2026  $1,001 - $15,000\n"
        "2     Apple Inc. (AAPL)     S 08/22/2026 08/26/2026  $15,001 - $50,000\n"
    )
    assert CongressFilter._extract_tickers(text) == ["TSLA", "AAPL"]


def test_index_failure_returns_empty(monkeypatch):
    cf = CongressFilter()

    def boom(year):
        raise CongressError("HTTP 503")

    monkeypatch.setattr(cf, "_load_index", boom)
    assert cf.recent_trades(["TSLA"], now=dt.date(2026, 10, 30)) == []
