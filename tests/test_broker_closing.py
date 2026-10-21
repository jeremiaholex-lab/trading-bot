"""Regression tests for closing positions that carry bracket orders.

Found by running the bot against a real Alpaca paper account: a position
opened with a bracket order has its shares *held* by the open stop-loss and
take-profit orders. Submitting a market sell without cancelling them first
fails with "insufficient qty available for order (requested: 13, available: 0)"
-- so every exit was silently rejected and the position could never be closed
by a signal.
"""
import pytest

from execution.alpaca_broker import AlpacaBroker, OrderResult


class _FakeOrder:
    def __init__(self, oid="ord-1", status="accepted"):
        self.id = oid
        self.status = status


class _FakeAlpacaClient:
    """Minimal stand-in for alpaca.trading.client.TradingClient.

    Mirrors the real API surface: `cancel_order_by_id`, `get_orders` and
    `submit_order`. (There is no `cancel_open_orders` on the real client --
    calling it raised AttributeError, so every close was attempted with the
    bracket orders still holding the shares.)
    """

    def __init__(self, fail_without_cancel=False):
        self.cancelled = []
        self.submitted = []
        self.fail_without_cancel = fail_without_cancel
        self.open_orders = []
        self.positions = []

    def get_orders(self, filter=None):
        return list(self.open_orders)

    def get_all_positions(self):
        return list(self.positions)

    def get_account(self):
        class _Acc:
            account_number = "ACC1"
            equity = 100000.0
            cash = 100000.0
            buying_power = 200000.0
            currency = "USD"
        return _Acc()

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(str(order_id))
        self.open_orders = [o for o in self.open_orders if str(o.id) != str(order_id)]

    def submit_order(self, request):
        # Model Alpaca's behaviour: shares held by an open bracket order
        # cannot be sold until that order is cancelled.
        if self.fail_without_cancel and not self.cancelled:
            raise RuntimeError(
                '{"code":40310000,"message":"insufficient qty available for order '
                '(requested: 13, available: 0)","held_for_orders":"13"}'
            )
        self.submitted.append(request)
        return _FakeOrder()


def _broker_with_client(monkeypatch, client):
    from config import Config

    monkeypatch.setattr(Config, "ALPACA_API_KEY", "PKtest")
    monkeypatch.setattr(Config, "ALPACA_SECRET_KEY", "secret")
    monkeypatch.setattr(Config, "ALPACA_LIVE", False)

    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker.client = client
    broker.is_simulated = False
    broker.sim_cash = 0.0
    broker.sim_positions = {}
    broker.order_counter = 1
    return broker


def test_closing_a_position_cancels_the_bracket_orders_first(monkeypatch):
    """The protective orders must be cancelled before the shares are sold."""
    client = _FakeAlpacaClient(fail_without_cancel=True)
    # A bracket order holding the shares.
    client.open_orders = [_FakeOrder(oid="stop-1"), _FakeOrder(oid="tp-1")]
    broker = _broker_with_client(monkeypatch, client)

    result = broker.place_close_position_order(symbol="AAPL", qty=13, reason="bearish")

    assert client.cancelled, (
        "the bracket stop/take-profit orders were not cancelled; Alpaca holds "
        "the shares for them and rejects the sell"
    )
    assert sorted(client.cancelled) == ["stop-1", "tp-1"]
    assert result.success is True, f"close order failed: {result.message}"
    assert len(client.submitted) == 1


def test_closing_a_position_works_with_no_open_orders(monkeypatch):
    """Closing a position with no protective orders must still work."""
    client = _FakeAlpacaClient()
    broker = _broker_with_client(monkeypatch, client)

    result = broker.place_close_position_order(symbol="AAPL", qty=13, reason="bearish")

    assert result.success is True, f"close order failed: {result.message}"
    assert len(client.submitted) == 1


def test_close_order_reports_failure_without_raising(monkeypatch):
    """A rejected close must come back as a failed result, not an exception."""
    class _AngryClient(_FakeAlpacaClient):
        def cancel_order_by_id(self, order_id):
            raise RuntimeError("cancel failed")

        def submit_order(self, request):
            raise RuntimeError("insufficient qty available for order (available: 0)")

    broker = _broker_with_client(monkeypatch, _AngryClient())

    result = broker.place_close_position_order(symbol="AAPL", qty=13, reason="test")

    assert isinstance(result, OrderResult)
    assert result.success is False
    assert "rejected" in result.message.lower()


def test_trailing_stop_cancels_the_old_stop_before_replacing_it(monkeypatch):
    """Moving a stop must cancel the existing stop order first."""
    from alpaca.trading.enums import OrderStatus, OrderType, OrderSide

    class _StopOrder:
        def __init__(self, oid):
            self.id = oid
            self.symbol = "NVDA"
            self.status = OrderStatus.NEW
            self.type = OrderType.STOP
            self.side = OrderSide.SELL
            self.qty = 10

    client = _FakeAlpacaClient()
    client.open_orders = [_StopOrder("old-stop-1")]

    class _Position:
        symbol = "NVDA"
        qty = 10
        avg_entry_price = 100.0
        current_price = 120.0
        unrealized_pl = 200.0
        unrealized_plpc = 0.2

    client.positions = [_Position()]
    broker = _broker_with_client(monkeypatch, client)

    ok = broker.update_trailing_stop("NVDA", 115.0)

    assert ok is True
    assert "old-stop-1" in client.cancelled, (
        "the previous stop order was not cancelled before the replacement was "
        "submitted; Alpaca would reject the new stop as a duplicate"
    )
    assert len(client.submitted) == 1


def test_simulator_close_removes_the_position(tmp_path):
    """In simulator mode closing must remove the position and return the cash."""
    broker = AlpacaBroker(force_simulator=True, state_file=tmp_path / "sim_state.json")
    broker.sim_cash = 100000.0
    broker.sim_positions = {}
    broker.place_bracket_buy_order(
        symbol="SPY", qty=10, entry_price=500.0,
        stop_loss_price=490.0, take_profit_price=520.0,
    )
    assert "SPY" in broker.sim_positions

    # The buy already took 5000 out of the cash.
    assert broker.sim_cash == 95000.0

    broker.sim_positions["SPY"].current_price = 510.0
    result = broker.place_close_position_order(symbol="SPY", qty=10, reason="test")

    assert result.success is True
    assert "SPY" not in broker.sim_positions
    assert broker.sim_cash == 95000.0 + 5100.0


def test_simulator_state_is_not_loaded_when_connected_to_alpaca(monkeypatch, tmp_path):
    """Live mode must not read the simulator's state file."""
    from config import Config

    state_file = tmp_path / "sim_state.json"
    state_file.write_text('{"cash": 12345.0, "positions": {}}', encoding="utf-8")

    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker.client = None
    broker.is_simulated = False
    broker.sim_cash = 0.0
    broker.sim_positions = {}
    broker.order_counter = 1
    broker.state_file = state_file

    broker._load_sim_state()

    assert broker.sim_cash == 0.0, (
        "the simulator state file was loaded into a live Alpaca session"
    )


def test_trailing_stop_keeps_the_take_profit_alive(monkeypatch):
    """Trailing the stop must not cancel the take-profit.

    Cancelling every open order for the symbol would silently remove the
    profit target and leave the position with no exit.
    """
    from alpaca.trading.enums import OrderStatus, OrderType, OrderSide

    class _StopOrder:
        def __init__(self, oid, symbol, stop_price):
            self.id = oid
            self.symbol = symbol
            self.status = OrderStatus.NEW
            self.type = OrderType.STOP
            self.side = OrderSide.SELL
            self.qty = 10
            self.stop_price = stop_price
            self.limit_price = None

    class _LimitOrder:
        def __init__(self, oid, symbol, limit_price):
            self.id = oid
            self.symbol = symbol
            self.status = OrderStatus.NEW
            self.type = OrderType.LIMIT
            self.side = OrderSide.SELL
            self.qty = 10
            self.stop_price = None
            self.limit_price = limit_price

    class _Position:
        symbol = "NVDA"
        qty = 10
        avg_entry_price = 100.0
        current_price = 120.0
        unrealized_pl = 200.0
        unrealized_plpc = 0.2

    client = _FakeAlpacaClient()
    client.open_orders = [
        _StopOrder("stop-1", "NVDA", 95.0),
        _LimitOrder("tp-1", "NVDA", 130.0),
    ]
    client.positions = [_Position()]
    broker = _broker_with_client(monkeypatch, client)

    ok = broker.update_trailing_stop("NVDA", 115.0)

    assert ok is True
    assert "stop-1" in client.cancelled
    assert "tp-1" not in client.cancelled, (
        "the take-profit order was cancelled while trailing the stop; the "
        "position would be left with no profit target"
    )


def test_live_positions_expose_their_protective_stop_price(monkeypatch):
    """The trailing-stop logic needs the live stop price.

    Alpaca keeps the protective stop as a separate open order; the position
    object does not carry its price. Without reading it back, the trailing
    stop silently skipped every live position.
    """
    from alpaca.trading.enums import OrderStatus, OrderType, OrderSide

    class _StopOrder:
        def __init__(self, oid, symbol, stop_price):
            self.id = oid
            self.symbol = symbol
            self.status = OrderStatus.NEW
            self.type = OrderType.STOP
            self.side = OrderSide.SELL
            self.qty = 10
            self.stop_price = stop_price
            self.limit_price = None

    class _Position:
        symbol = "NVDA"
        qty = 10
        avg_entry_price = 100.0
        current_price = 120.0
        unrealized_pl = 200.0
        unrealized_plpc = 0.2

    client = _FakeAlpacaClient()
    client.open_orders = [_StopOrder("stop-1", "NVDA", 95.0)]
    client.positions = [_Position()]
    broker = _broker_with_client(monkeypatch, client)

    positions = broker.get_open_positions()

    assert positions["NVDA"]["stop_loss"] == 95.0, (
        "the live stop price was not read back from the open stop order; the "
        "trailing stop would silently skip every live position"
    )
