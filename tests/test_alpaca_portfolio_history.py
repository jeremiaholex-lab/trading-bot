from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from alpaca.trading.models import PortfolioHistory

from execution.alpaca_broker import AlpacaBroker


def make_broker(*, simulated=False, history=None, created_at=None):
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker.is_simulated = simulated
    broker.client = MagicMock()
    if history is not None:
        broker.client.get_portfolio_history.return_value = history
    if created_at is not None:
        broker.client.get_account.return_value = SimpleNamespace(created_at=created_at)
    return broker


def test_get_portfolio_history_forwards_request_and_normalizes_points():
    history = PortfolioHistory(
        timestamp=[1_762_000_000, 1_762_003_600],
        equity=[100_000.0, 101_250.0],
        profit_loss=[0.0, 1_250.0],
        profit_loss_pct=[0.0, 0.0125],
        base_value=100_000.0,
        timeframe="1H",
        cashflow={},
    )
    broker = make_broker(history=history)

    result = broker.get_portfolio_history("1M", "1H")

    assert result == {
        "available": True,
        "period": "1M",
        "timeframe": "1H",
        "base_value": 100_000.0,
        "points": [
            {"timestamp": 1_762_000_000, "equity": 100_000.0, "profit_loss": 0.0, "profit_loss_pct": 0.0},
            {"timestamp": 1_762_003_600, "equity": 101_250.0, "profit_loss": 1_250.0, "profit_loss_pct": 0.0125},
        ],
    }
    request = broker.client.get_portfolio_history.call_args.args[0]
    assert request.period == "1M"
    assert request.timeframe == "1H"


def test_get_portfolio_history_maps_one_year_to_api_annual_period():
    history = PortfolioHistory(
        timestamp=[1_762_000_000], equity=[100_000.0], profit_loss=[0.0],
        profit_loss_pct=[0.0], base_value=100_000.0, timeframe="1D", cashflow={},
    )
    broker = make_broker(history=history)

    broker.get_portfolio_history("1Y", "1D")

    request = broker.client.get_portfolio_history.call_args.args[0]
    assert request.period == "1A"
    assert request.timeframe == "1D"


def test_get_portfolio_history_all_uses_account_creation_to_now():
    created_at = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    history = PortfolioHistory(
        timestamp=[1_762_000_000], equity=[100_000.0], profit_loss=[0.0],
        profit_loss_pct=[0.0], base_value=100_000.0, timeframe="1D", cashflow={},
    )
    broker = make_broker(history=history, created_at=created_at)

    result = broker.get_portfolio_history("All", "1D")

    request = broker.client.get_portfolio_history.call_args.args[0]
    assert request.start == created_at
    assert isinstance(request.end, datetime)
    assert request.end.tzinfo is not None
    assert request.end >= created_at
    assert request.period is None
    assert request.timeframe == "1D"
    assert result["period"] == "All"


def test_get_portfolio_history_reports_unavailable_for_simulator():
    broker = make_broker(simulated=True)

    result = broker.get_portfolio_history("1M", "1D")

    assert result["available"] is False
    assert "simulator" in result["error"].lower()
    broker.client.get_portfolio_history.assert_not_called()


def test_get_portfolio_history_preserves_missing_series_as_null():
    history = PortfolioHistory(
        timestamp=[1_762_000_000, 1_762_003_600],
        equity=[100_000.0, 101_250.0],
        profit_loss=[0.0],
        profit_loss_pct=[0.0, 0.0125],
        base_value=100_000.0,
        timeframe="1H",
        cashflow={},
    )
    broker = make_broker(history=history)

    result = broker.get_portfolio_history("1W", "1H")

    assert result["points"][1] == {
        "timestamp": 1_762_003_600,
        "equity": 101_250.0,
        "profit_loss": None,
        "profit_loss_pct": 0.0125,
    }
