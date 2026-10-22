"""Tests for protective order (stop-loss / take-profit) integrity.

This is the most expensive failure mode in the bot, and it was silent.

A bracket order was submitted WITHOUT `order_class=OrderClass.BRACKET`.
Alpaca accepted the request, returned success, and created NO protective
legs. The position existed with a stop-loss and a take-profit that did
not exist. Every status display said the position was fine: it appeared
in the dashboard, counted toward MAX_OPEN_POSITIONS, and reported a
plausible unrealised P&L. Only the absence of protective orders -- which
nothing looked for -- distinguished it from a protected position.

The trailing-stop path had the mirror-image bug: it CANCELLED the stop
and then submitted a replacement. The replacement was rejected with
"insufficient qty available" because the cancelled order's shares were
still held. The position was left permanently bare, and it was being
trailed precisely because it was profitable.

These tests pin both behaviours.
"""
import pytest

from execution.alpaca_broker import AlpacaBroker


class _FakeOrder:
    def __init__(self, order_type, side="sell", qty=1, stop_price=None, limit_price=None,
                 order_id="o1", status="held", symbol="SPY"):
        self.id = order_id
        self.type = order_type
        self.side = side
        self.qty = qty
        self.stop_price = stop_price
        self.limit_price = limit_price
        self.status = status
        self.symbol = symbol


class _FakeClient:
    """A trading client that records what was submitted."""

    def __init__(self, orders_on_file=None, account=None):
        self.submitted = []
        self.cancelled = []
        self.replaced = []
        self._orders = list(orders_on_file or [])
        self._account = account or _FakeAccount()
        self.bracket_legs_appear = True

    def get_account(self):
        return self._account

    def get_all_positions(self):
        return []

    def get_orders(self, request=None):
        return list(self._orders)

    def submit_order(self, req):
        fields = req.to_request_fields() if hasattr(req, "to_request_fields") else dict(req)
        self.submitted.append(fields)
        if fields.get("order_class") is not None and self.bracket_legs_appear:
            self._orders.append(_FakeOrder("stop", qty=fields.get("qty", 1),
                                           stop_price=fields.get("stop_loss", {}).get("stop_price")))
            self._orders.append(_FakeOrder("limit", qty=fields.get("qty", 1),
                                           limit_price=fields.get("take_profit", {}).get("limit_price")))
        return _FakeOrder("market")

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(str(order_id))
        self._orders = [o for o in self._orders if str(o.id) != str(order_id)]

    def replace_order_by_id(self, order_id, order_data):
        self.replaced.append((str(order_id), order_data))
        return _FakeOrder("stop")


class _FakeAccount:
    equity = 100000.0
    cash = 50000.0
    buying_power = 100000.0
    currency = "USD"
    account_number = "ACC1"
    last_equity = 100000.0


def _broker(monkeypatch, client):
    monkeypatch.setattr("alpaca.trading.client.TradingClient", lambda **kw: client)
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker.client = client
    broker.is_simulated = False
    broker._sim_positions = {}
    broker._sim_cash = 100000.0
    broker.sim_cash = 100000.0
    broker.sim_positions = {}
    broker.sim_last_equity = 100000.0
    broker.order_counter = 1
    return broker


# ----------------------------------------------------------------------
# The bracket order must actually carry its protective legs.
# ----------------------------------------------------------------------
def test_bracket_order_declares_the_bracket_order_class(monkeypatch):
    """The order MUST be submitted with order_class=BRACKET.

    Without it Alpaca accepts the request and creates no legs. The
    request succeeds, the position opens, and it has no protection.
    """
    client = _FakeClient()
    broker = _broker(monkeypatch, client)

    broker.place_bracket_buy_order("SPY", 10, 500.0, 490.0, 520.0)

    assert len(client.submitted) == 1
    fields = client.submitted[0]
    assert fields.get("order_class") is not None, (
        "bracket order submitted without order_class=BRACKET; Alpaca will "
        "accept it and create NO protective stop-loss or take-profit"
    )
    assert str(fields["order_class"]) in ("bracket", "OrderClass.BRACKET")
    assert fields.get("stop_loss", {}).get("stop_price") == 490.0
    assert fields.get("take_profit", {}).get("limit_price") == 520.0


def test_bracket_order_failure_is_reported_when_legs_are_missing(monkeypatch):
    """If the legs do not appear, the call must not report plain success."""
    client = _FakeClient()
    client.bracket_legs_appear = False
    broker = _broker(monkeypatch, client)

    result = broker.place_bracket_buy_order("SPY", 10, 500.0, 490.0, 520.0)

    assert result.success is False, (
        "reported success even though no protective legs were created; "
        "the position is unprotected and the operator was never told"
    )
    assert "protect" in result.message.lower()


def test_bracket_order_reports_success_when_legs_are_present(monkeypatch):
    """The normal path must still report success."""
    client = _FakeClient()
    broker = _broker(monkeypatch, client)

    result = broker.place_bracket_buy_order("SPY", 10, 500.0, 490.0, 520.0)

    assert result.success is True


# ----------------------------------------------------------------------
# The trailing stop must never cancel-then-resubmit.
# ----------------------------------------------------------------------
def test_trailing_stop_replaces_the_order_instead_of_cancelling_first(monkeypatch):
    """Trailing must PATCH the existing stop, not cancel and resubmit.

    Cancel-then-submit leaves a window with no protection at all, and
    the replacement is rejected because the cancelled order's shares are
    still held. The result is a permanently unprotected position.
    """
    client = _FakeClient(orders_on_file=[_FakeOrder("stop", qty=10, stop_price=490.0, order_id="stop-1")])
    broker = _broker(monkeypatch, client)

    monkeypatch.setattr(broker, "get_open_positions", lambda: {
        "SPY": {"symbol": "SPY", "qty": 10, "entry_price": 500.0,
                "current_price": 520.0, "unrealized_pl": 200.0, "unrealized_plpc": 4.0}
    })

    ok = broker.update_trailing_stop("SPY", 505.0)

    assert ok is True
    assert client.replaced, (
        "the stop was not replaced; cancel-then-resubmit leaves the "
        "position unprotected while it is being trailed"
    )
    assert not client.cancelled, (
        "the existing stop was cancelled before the replacement was "
        "submitted, creating a window with no protection"
    )


def test_trailing_stop_submits_a_fresh_order_when_none_exists(monkeypatch):
    """If the stop went missing, a new one must be submitted."""
    client = _FakeClient(orders_on_file=[])
    broker = _broker(monkeypatch, client)

    monkeypatch.setattr(broker, "get_open_positions", lambda: {
        "SPY": {"symbol": "SPY", "qty": 10, "entry_price": 500.0,
                "current_price": 520.0, "unrealized_pl": 200.0, "unrealized_plpc": 4.0}
    })

    ok = broker.update_trailing_stop("SPY", 505.0)

    assert ok is True
    assert client.submitted, "no stop order was submitted for an unprotected position"
    assert client.submitted[0].get("stop_price") == 505.0


# ----------------------------------------------------------------------
# The audit.
# ----------------------------------------------------------------------
def test_audit_restores_protection_for_a_bare_position(monkeypatch):
    """A position with no protective orders must be given some."""
    client = _FakeClient(orders_on_file=[])
    broker = _broker(monkeypatch, client)

    monkeypatch.setattr(broker, "get_open_positions", lambda: {
        "SPY": {"symbol": "SPY", "qty": 10, "entry_price": 500.0,
                "current_price": 520.0, "unrealized_pl": 200.0, "unrealized_plpc": 4.0}
    })

    problems = broker.ensure_protective_orders()

    assert "SPY" in problems, "an unprotected position was not reported"
    assert len(client.submitted) == 1
    fields = client.submitted[0]
    assert fields.get("stop_loss", {}).get("stop_price") is not None
    assert fields.get("take_profit", {}).get("limit_price") is not None


def test_audit_leaves_a_protected_position_alone(monkeypatch):
    """A position that already has both legs must not be touched."""
    client = _FakeClient(orders_on_file=[
        _FakeOrder("stop", qty=10, stop_price=490.0, order_id="s1"),
        _FakeOrder("limit", qty=10, limit_price=520.0, order_id="l1"),
    ])
    broker = _broker(monkeypatch, client)

    monkeypatch.setattr(broker, "get_open_positions", lambda: {
        "SPY": {"symbol": "SPY", "qty": 10, "entry_price": 500.0,
                "current_price": 520.0, "unrealized_pl": 200.0, "unrealized_plpc": 4.0}
    })

    problems = broker.ensure_protective_orders()

    assert problems == {}
    assert not client.submitted


def test_audit_uses_a_single_oco_order_for_both_legs(monkeypatch):
    """Both legs must come from ONE order.

    Two separate sell orders cannot both reserve the same shares.
    Alpaca reserves the quantity per order, so the second is rejected
    with "insufficient qty available" and the position ends up with
    only one leg.
    """
    client = _FakeClient(orders_on_file=[])
    broker = _broker(monkeypatch, client)

    monkeypatch.setattr(broker, "get_open_positions", lambda: {
        "SPY": {"symbol": "SPY", "qty": 10, "entry_price": 500.0,
                "current_price": 520.0, "unrealized_pl": 200.0, "unrealized_plpc": 4.0}
    })

    broker.ensure_protective_orders()

    assert len(client.submitted) == 1, (
        "protection was submitted as more than one order; the second one "
        "cannot reserve the same shares and will be rejected"
    )
    fields = client.submitted[0]
    assert fields.get("stop_loss", {}).get("stop_price") is not None
    assert fields.get("take_profit", {}).get("limit_price") is not None


# ----------------------------------------------------------------------
# HELD stop-losses must be visible.
#
# Alpaca's `status=open` filter excludes orders in the HELD status, and
# a bracket stop-loss is exactly that -- it is held while the entry
# fills. Every protection check in this bot used `status=open` and was
# therefore blind to stop-losses:
#
#   * the trailing-stop logic could not read the current stop price and
#     skipped every position,
#   * the protective-order audit believed every position was unprotected,
#     then tried to "restore" a stop that was already there -- cancelling
#     the real one and leaving the position bare.
#
# Only `status=all` surfaces HELD orders, so the query and the
# live-status filter are both pinned here.
# ----------------------------------------------------------------------
def test_working_orders_include_held_stop_losses(monkeypatch):
    """A HELD stop-loss must be visible to the protection logic."""
    client = _HeldFilteringClient(orders_on_file=[
        _FakeOrder("stop", qty=10, stop_price=490.0, order_id="s1", status="held"),
        _FakeOrder("limit", qty=10, limit_price=520.0, order_id="l1", status="new"),
    ])
    broker = _broker(monkeypatch, client)

    orders = broker._get_symbol_orders("SPY")

    types = sorted(str(o.type) for o in orders)
    assert len(orders) == 2, (
        f"expected both the HELD stop and the NEW take-profit, got {types}; "
        "a HELD stop-loss is invisible to the protection logic"
    )


def test_working_orders_exclude_filled_and_cancelled_orders(monkeypatch):
    """Dead orders must not be treated as live protection."""
    client = _HeldFilteringClient(orders_on_file=[
        _FakeOrder("stop", qty=10, stop_price=490.0, order_id="s1", status="filled"),
        _FakeOrder("limit", qty=10, limit_price=520.0, order_id="l2", status="canceled"),
        _FakeOrder("stop", qty=10, stop_price=495.0, order_id="s2", status="held"),
    ])
    broker = _broker(monkeypatch, client)

    orders = broker._get_symbol_orders("SPY")

    ids = sorted(str(o.id) for o in orders)
    assert ids == ["s2"], f"expected only the live HELD stop, got {ids}"


def test_audit_does_not_disturb_a_position_protected_by_a_held_stop(monkeypatch):
    """The audit must not cancel a real HELD stop and re-create it.

    This is the exact failure that left SGOV unprotected: the audit
    could not see the HELD stop, concluded the position was bare,
    cancelled the real stop, and the replacement was rejected because
    the shares were still held.
    """
    client = _HeldFilteringClient(orders_on_file=[
        _FakeOrder("stop", qty=10, stop_price=490.0, order_id="s1", status="held"),
        _FakeOrder("limit", qty=10, limit_price=520.0, order_id="l1", status="new"),
    ])
    broker = _broker(monkeypatch, client)

    monkeypatch.setattr(broker, "get_open_positions", lambda: {
        "SPY": {"symbol": "SPY", "qty": 10, "entry_price": 500.0,
                "current_price": 520.0, "unrealized_pl": 200.0, "unrealized_plpc": 4.0}
    })

    problems = broker.ensure_protective_orders()

    assert problems == {}, (
        f"a position protected by a HELD stop was reported as unprotected: {problems}"
    )
    assert not client.cancelled, (
        "the audit cancelled the existing HELD stop; the replacement would be "
        "rejected for insufficient qty, leaving the position unprotected"
    )
    assert not client.submitted


def test_protective_prices_include_held_stops(monkeypatch):
    """The trailing-stop logic reads the stop price from here.

    It must see the HELD stop, or every position is skipped because it
    looks like it has no protective stop at all.
    """
    client = _HeldFilteringClient(orders_on_file=[
        _FakeOrder("stop", qty=10, stop_price=490.0, order_id="s1", status="held"),
        _FakeOrder("limit", qty=10, limit_price=520.0, order_id="l1", status="new"),
    ])
    broker = _broker(monkeypatch, client)

    stops, limits = broker._get_protective_order_prices()

    assert stops.get("SPY") == 490.0, (
        "the HELD stop-loss was not reported; the trailing-stop logic would "
        "skip every position"
    )
    assert limits.get("SPY") == 520.0


class _HeldFilteringClient(_FakeClient):
    """A client that mimics Alpaca's real status filtering.

    Alpaca's `status=open` query does NOT return orders in the HELD
    status, and a bracket stop-loss is HELD while the position is open.
    A client that returned everything regardless of the filter would let
    the HELD-visibility bug slip through.
    """

    def get_orders(self, request=None):
        status = getattr(request, "status", "open")
        if status == "all":
            return list(self._orders)
        if status == "open":
            return [o for o in self._orders if str(o.status).lower() == "new"]
        return [o for o in self._orders
                if str(o.status).lower() in ("filled", "canceled", "expired")]
