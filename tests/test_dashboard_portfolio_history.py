import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import dashboard.data_fetcher as data_fetcher
import dashboard_server


HISTORY = {
    "available": True,
    "period": "1M",
    "timeframe": "1D",
    "base_value": 100_000.0,
    "points": [{"timestamp": 1_762_000_000, "equity": 101_250.0, "profit_loss": 1_250.0, "profit_loss_pct": 0.0125}],
}


def install_fake_broker(monkeypatch, response=HISTORY):
    calls = []

    class FakeBroker:
        def get_portfolio_history(self, period, timeframe):
            calls.append((period, timeframe))
            return response

    monkeypatch.setattr(data_fetcher, "_orchestrator", SimpleNamespace(broker=FakeBroker()))
    monkeypatch.setattr(data_fetcher, "_cache", {})
    monkeypatch.setattr(data_fetcher, "_key_locks", {})
    return calls


def test_history_cache_is_keyed_by_period_and_timeframe(monkeypatch):
    calls = install_fake_broker(monkeypatch)

    first = data_fetcher.get_portfolio_history("1M", "1D")
    cached = data_fetcher.get_portfolio_history("1M", "1D")
    weekly_intraday = data_fetcher.get_portfolio_history("1W", "15Min")

    assert first == HISTORY
    assert cached == HISTORY
    assert weekly_intraday == HISTORY
    assert calls == [("1M", "1D"), ("1W", "15Min")]


def test_history_route_passes_validated_parameters(monkeypatch):
    seen = []
    monkeypatch.setattr(
        dashboard_server.df,
        "get_portfolio_history",
        lambda period, timeframe: seen.append((period, timeframe)) or HISTORY,
    )

    result = asyncio.run(dashboard_server.api_portfolio_history(period="1M", timeframe="1D"))

    assert result == HISTORY
    assert seen == [("1M", "1D")]


def test_history_route_all_range_uses_daily_resolution(monkeypatch):
    seen = []
    monkeypatch.setattr(
        dashboard_server.df,
        "get_portfolio_history",
        lambda period, timeframe: seen.append((period, timeframe)) or {**HISTORY, "period": "All"},
    )

    result = asyncio.run(dashboard_server.api_portfolio_history(period="All", timeframe="1D"))

    assert result["period"] == "All"
    assert seen == [("All", "1D")]


@pytest.mark.parametrize(
    ("period", "timeframe"),
    [("2M", "1D"), ("1M", "10Min"), ("3M", "1H"), ("All", "1H")],
)
def test_history_route_rejects_invalid_range_and_long_intraday(monkeypatch, period, timeframe):
    calls = []
    monkeypatch.setattr(
        dashboard_server.df,
        "get_portfolio_history",
        lambda *args: calls.append(args) or HISTORY,
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(dashboard_server.api_portfolio_history(period=period, timeframe=timeframe))

    assert caught.value.status_code == 422
    assert calls == []


def test_history_route_keeps_simulator_unavailable_state(monkeypatch):
    unavailable = {
        "available": False,
        "period": "1M",
        "timeframe": "1D",
        "base_value": None,
        "points": [],
        "error": "Alpaca portfolio history is unavailable for the local simulator.",
    }
    monkeypatch.setattr(dashboard_server.df, "get_portfolio_history", lambda *_: unavailable)

    result = asyncio.run(dashboard_server.api_portfolio_history(period="1M", timeframe="1D"))

    assert result == unavailable
