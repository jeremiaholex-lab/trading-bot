import time
import pytest
from unittest.mock import MagicMock
from execution.order_types import (
    OrderTypeEnum,
    AlgoStatus,
    AlgoHandle,
    SimpleOrderExecutor,
    AlgoOrderManager,
    _vwap_slice_fractions,
)
from execution.alpaca_broker import AlpacaBroker, OrderResult


def test_order_types_enum():
    assert OrderTypeEnum.LIMIT == "limit"
    assert OrderTypeEnum.STOP_LIMIT == "stop_limit"
    assert OrderTypeEnum.TWAP == "twap"
    assert OrderTypeEnum.VWAP == "vwap"
    assert OrderTypeEnum.ICEBERG == "iceberg"


def test_vwap_slice_fractions():
    fractions = _vwap_slice_fractions(5)
    assert len(fractions) == 5
    assert math_close(sum(fractions), 1.0)
    assert _vwap_slice_fractions(0) == []


def math_close(a, b, tol=1e-5):
    return abs(a - b) < tol


def test_simple_order_executor_simulator(tmp_path):
    broker = AlpacaBroker(force_simulator=True, state_file=tmp_path / "sim.json")
    broker.fill_engine.latency.block = False
    executor = SimpleOrderExecutor(broker)

    # Limit buy
    res = executor.place_limit_buy("AAPL", 5, 150.0, 140.0, 170.0)
    assert res.success is True
    assert "AAPL" in broker.sim_positions

    # Stop limit buy
    res2 = executor.place_stop_limit_buy("MSFT", 5, 300.0, 301.0, 290.0, 320.0)
    assert res2.success is True
    assert "MSFT" in broker.sim_positions


def test_simple_order_executor_alpaca_mock():
    mock_broker = MagicMock()
    mock_broker.is_simulated = False
    mock_order = MagicMock()
    mock_order.id = "alpaca-123"
    mock_broker.client.submit_order.return_value = mock_order

    executor = SimpleOrderExecutor(mock_broker)
    res = executor.place_limit_buy("AAPL", 10, 150.0, 140.0, 170.0)
    assert res.success is True
    assert res.order_id == "alpaca-123"
    assert mock_broker.client.submit_order.called


def test_algo_manager_twap():
    submitted = []

    def mock_place(sym, qty, price, sl, tp):
        submitted.append((sym, qty, price))
        return OrderResult(
            success=True, order_id=f"ord-{len(submitted)}", symbol=sym,
            qty=qty, entry_price=price, stop_loss=sl, take_profit=tp, message="OK"
        )

    def mock_get_price(sym):
        return 100.0

    mgr = AlgoOrderManager(mock_place, mock_get_price)
    handle = mgr.place_twap(
        symbol="SPY",
        qty=10,
        duration_minutes=0.001,  # very short duration for fast test
        stop_loss=90.0,
        take_profit=120.0,
        n_slices=2,
    )
    # Wait for completion
    for _ in range(50):
        if handle.status == AlgoStatus.COMPLETE:
            break
        time.sleep(0.02)

    assert handle.status == AlgoStatus.COMPLETE
    assert handle.filled_qty == 10
    assert len(handle.child_order_ids) == 2
    assert handle.avg_fill_price == 100.0
    d = handle.to_dict()
    assert d["status"] == "complete"


def test_algo_manager_vwap():
    submitted = []

    def mock_place(sym, qty, price, sl, tp):
        submitted.append((sym, qty, price))
        return OrderResult(
            success=True, order_id=f"ord-{len(submitted)}", symbol=sym,
            qty=qty, entry_price=price, stop_loss=sl, take_profit=tp, message="OK"
        )

    mgr = AlgoOrderManager(mock_place, lambda sym: 200.0)
    handle = mgr.place_vwap(
        symbol="QQQ",
        qty=20,
        duration_minutes=0.001,
        stop_loss=190.0,
        take_profit=220.0,
        n_slices=3,
    )
    for _ in range(50):
        if handle.status == AlgoStatus.COMPLETE:
            break
        time.sleep(0.02)

    assert handle.status == AlgoStatus.COMPLETE
    assert handle.filled_qty == 20


def test_algo_manager_iceberg():
    submitted = []

    def mock_place(sym, qty, price, sl, tp):
        submitted.append((sym, qty, price))
        return OrderResult(
            success=True, order_id=f"ord-{len(submitted)}", symbol=sym,
            qty=qty, entry_price=price, stop_loss=sl, take_profit=tp, message="OK"
        )

    mgr = AlgoOrderManager(mock_place, lambda sym: 50.0)
    handle = mgr.place_iceberg(
        symbol="AMD",
        total_qty=25,
        display_qty=10,
        stop_loss=45.0,
        take_profit=60.0,
    )
    # Wait for first tranche
    for _ in range(30):
        if handle.filled_qty >= 10:
            break
        time.sleep(0.02)

    # Cancel before finish
    handle.cancel()
    assert handle.is_cancel_requested()
    for _ in range(30):
        if handle.status in (AlgoStatus.CANCELLED, AlgoStatus.PARTIAL):
            break
        time.sleep(0.02)

    assert handle.status in (AlgoStatus.CANCELLED, AlgoStatus.PARTIAL)


def test_alpaca_broker_order_methods(tmp_path):
    broker = AlpacaBroker(force_simulator=True, state_file=tmp_path / "sim.json")
    broker.fill_engine.latency.block = False

    # limit buy method
    res = broker.place_limit_buy("AAPL", 5, 150.0, 140.0, 160.0)
    assert res.success is True

    # stop limit buy method
    res2 = broker.place_stop_limit_buy("AAPL", 5, 150.0, 151.0, 140.0, 160.0)
    assert res2.success is True

    # algo manager init
    mock_md = MagicMock()
    mock_md.get_latest_price.return_value = 150.0
    broker.init_algo_manager(mock_md)
    assert broker.algo_manager is not None

    h = broker.place_twap_buy("AAPL", 10, 0.001, 140.0, 160.0, n_slices=2)
    assert isinstance(h, AlgoHandle)
