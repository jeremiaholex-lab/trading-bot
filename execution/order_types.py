"""
Advanced Order Type Execution Layer
====================================
Implements limit, stop-limit, TWAP, VWAP, and iceberg orders.
Works in both Alpaca Paper Cloud mode and local simulator mode.

Order Types
-----------
  LimitOrder       — buy at or below limit_price
  StopLimitOrder   — triggered at stop_price, then fills at limit_price
  TWAPOrder        — slices qty evenly over duration_minutes (time-weighted)
  VWAPOrder        — volume-weighted slices using intraday volume profile
  IcebergOrder     — shows only display_qty; resubmits hidden qty in tranches

All algo orders (TWAP/VWAP/Iceberg) run synchronously in a background thread
managed by AlgoOrderManager. Each algo order returns an AlgoHandle that can
be cancelled or awaited.

Design constraints
------------------
  - Never modifies Alpaca live-mode safety checks in alpaca_broker.py
  - Works with existing OrderResult dataclass
  - Falls back gracefully in simulator mode (uses market orders internally)
  - Config flags: DEFAULT_ORDER_TYPE, TWAP_SLICES, ICEBERG_DISPLAY_PCT
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class OrderTypeEnum(str, Enum):
    MARKET     = "market"
    LIMIT      = "limit"
    STOP_LIMIT = "stop_limit"
    TWAP       = "twap"
    VWAP       = "vwap"
    ICEBERG    = "iceberg"


class AlgoStatus(str, Enum):
    PENDING    = "pending"
    RUNNING    = "running"
    PARTIAL    = "partial"       # some slices filled
    COMPLETE   = "complete"
    CANCELLED  = "cancelled"
    FAILED     = "failed"


# ---------------------------------------------------------------------------
# AlgoHandle — returned by all algo orders
# ---------------------------------------------------------------------------

@dataclass
class AlgoHandle:
    """Tracks an in-progress algorithmic order."""
    algo_id: str
    symbol: str
    total_qty: int
    order_type: OrderTypeEnum
    status: AlgoStatus = AlgoStatus.PENDING
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    child_order_ids: List[str] = field(default_factory=list)
    error_message: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def cancel(self) -> None:
        """Signal this algo order to stop after the current slice."""
        self._cancel_event.set()
        logger.info(f"[{self.order_type}] Cancel requested for {self.algo_id} ({self.symbol})")

    def is_cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def to_dict(self) -> dict:
        return {
            "algo_id": self.algo_id,
            "symbol": self.symbol,
            "total_qty": self.total_qty,
            "order_type": self.order_type.value,
            "status": self.status.value,
            "filled_qty": self.filled_qty,
            "avg_fill_price": round(self.avg_fill_price, 4),
            "child_orders": len(self.child_order_ids),
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error_message,
        }


# ---------------------------------------------------------------------------
# Intraday volume profile for VWAP slicing
# ---------------------------------------------------------------------------

# Approximate fraction of daily volume traded in each 30-min bucket
# (US equity market composite, 09:30–16:00 = 13 half-hour periods)
_VOLUME_PROFILE: List[float] = [
    0.14,  # 09:30 – 10:00  (heavy open)
    0.09,  # 10:00 – 10:30
    0.07,  # 10:30 – 11:00
    0.06,  # 11:00 – 11:30
    0.06,  # 11:30 – 12:00
    0.05,  # 12:00 – 12:30  (lunch lull)
    0.05,  # 12:30 – 13:00
    0.05,  # 13:00 – 13:30
    0.06,  # 13:30 – 14:00
    0.07,  # 14:00 – 14:30
    0.07,  # 14:30 – 15:00
    0.09,  # 15:00 – 15:30
    0.14,  # 15:30 – 16:00  (heavy close)
]


def _vwap_slice_fractions(n_slices: int) -> List[float]:
    """Return n_slices fractions that sum to 1.0, following the volume profile."""
    if n_slices <= 0:
        return []
    # Distribute profile across requested slices
    profile_len = len(_VOLUME_PROFILE)
    fractions: List[float] = []
    for i in range(n_slices):
        # Sample the profile at evenly spaced points
        idx = int(i * profile_len / n_slices)
        fractions.append(_VOLUME_PROFILE[idx])
    # Normalize
    total = sum(fractions)
    return [f / total for f in fractions]


# ---------------------------------------------------------------------------
# PlaceOrderFn — type alias for the callback that submits one child order
# ---------------------------------------------------------------------------

# Signature: (symbol, qty, entry_price, stop_loss, take_profit) -> OrderResult
PlaceOrderFn = Callable[[str, int, float, float, float], object]

# Signature: () -> float
GetPriceFn = Callable[[], Optional[float]]


# ---------------------------------------------------------------------------
# SimpleOrderExecutor — handles limit and stop-limit on Alpaca or simulator
# ---------------------------------------------------------------------------

class SimpleOrderExecutor:
    """
    Executes limit and stop-limit orders.

    For Alpaca (live/paper): submits native order types via the trading API.
    For local simulator: degrades gracefully to a priced market order since
    the simulator does not model order books.
    """

    def __init__(self, broker: object) -> None:
        """
        Parameters
        ----------
        broker : AlpacaBroker instance
        """
        self._broker = broker

    def place_limit_buy(
        self,
        symbol: str,
        qty: int,
        limit_price: float,
        stop_loss: float,
        take_profit: float,
    ) -> object:
        """Submit a limit buy order with bracket protective legs."""
        from execution.alpaca_broker import OrderResult

        if getattr(self._broker, "is_simulated", True):
            # Simulator: treat limit_price as the fill price
            logger.info(
                f"[LIMIT] Simulator fill for {symbol}: qty={qty} "
                f"limit={limit_price:.2f} (market order at limit price)"
            )
            return self._broker.place_bracket_buy_order(
                symbol, qty, limit_price, stop_loss, take_profit
            )

        # Alpaca: native limit order
        try:
            from alpaca.trading.requests import LimitOrderRequest, TakeProfitRequest, StopLossRequest
            from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

            req = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=round(take_profit, 2)),
                stop_loss=StopLossRequest(stop_price=round(stop_loss, 2)),
                client_order_id=f"bot-{uuid.uuid4().hex}",
            )
            order = self._broker._submit_order_once(req, f"limit_buy({symbol})")
            logger.info(f"[LIMIT] Submitted limit buy {symbol} qty={qty} @ {limit_price:.2f} → {order.id}")
            return OrderResult(
                success=True,
                order_id=str(order.id),
                symbol=symbol,
                qty=qty,
                entry_price=limit_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                message=f"Limit buy submitted @ {limit_price:.2f}",
            )
        except Exception as exc:
            logger.error(f"[LIMIT] Failed to submit limit buy {symbol}: {exc}")
            return OrderResult(
                success=False, order_id="", symbol=symbol, qty=qty,
                entry_price=limit_price, stop_loss=stop_loss, take_profit=take_profit,
                message=f"Limit buy failed: {exc}",
            )

    def place_stop_limit_buy(
        self,
        symbol: str,
        qty: int,
        stop_price: float,
        limit_price: float,
        stop_loss: float,
        take_profit: float,
    ) -> object:
        """Submit a stop-limit buy: triggered at stop_price, fills at limit_price."""
        from execution.alpaca_broker import OrderResult

        if getattr(self._broker, "is_simulated", True):
            # Simulator: treat limit_price as fill price (no order-book simulation)
            logger.info(
                f"[STOP-LIMIT] Simulator fill for {symbol}: qty={qty} "
                f"stop={stop_price:.2f} limit={limit_price:.2f}"
            )
            return self._broker.place_bracket_buy_order(
                symbol, qty, limit_price, stop_loss, take_profit
            )

        # Alpaca: native stop-limit order
        try:
            from alpaca.trading.requests import StopLimitOrderRequest, TakeProfitRequest, StopLossRequest
            from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

            req = StopLimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                stop_price=round(stop_price, 2),
                limit_price=round(limit_price, 2),
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=round(take_profit, 2)),
                stop_loss=StopLossRequest(stop_price=round(stop_loss, 2)),
                client_order_id=f"bot-{uuid.uuid4().hex}",
            )
            order = self._broker._submit_order_once(req, f"stop_limit_buy({symbol})")
            logger.info(
                f"[STOP-LIMIT] Submitted stop-limit buy {symbol} qty={qty} "
                f"stop={stop_price:.2f} limit={limit_price:.2f} → {order.id}"
            )
            return OrderResult(
                success=True,
                order_id=str(order.id),
                symbol=symbol,
                qty=qty,
                entry_price=limit_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                message=f"Stop-limit buy submitted stop={stop_price:.2f} limit={limit_price:.2f}",
            )
        except Exception as exc:
            logger.error(f"[STOP-LIMIT] Failed to submit stop-limit buy {symbol}: {exc}")
            return OrderResult(
                success=False, order_id="", symbol=symbol, qty=qty,
                entry_price=limit_price, stop_loss=stop_loss, take_profit=take_profit,
                message=f"Stop-limit buy failed: {exc}",
            )


# ---------------------------------------------------------------------------
# AlgoOrderManager — runs TWAP / VWAP / Iceberg in background threads
# ---------------------------------------------------------------------------

class AlgoOrderManager:
    """
    Manages algorithmic order execution (TWAP, VWAP, Iceberg).

    Each algo order runs in a daemon thread. Use the returned AlgoHandle
    to monitor progress or cancel.

    Thread safety: handles dict is guarded by a lock. Individual algo threads
    update their own handle; no shared mutable state between threads.
    """

    def __init__(self, place_order_fn: PlaceOrderFn, get_price_fn: GetPriceFn) -> None:
        """
        Parameters
        ----------
        place_order_fn : callable(symbol, qty, price, stop_loss, take_profit) -> OrderResult
        get_price_fn   : callable(symbol) -> float | None — real-time price lookup
        """
        self._place = place_order_fn
        self._get_price = get_price_fn
        self._handles: Dict[str, AlgoHandle] = {}
        self._lock = threading.Lock()

    # ── Public API ──────────────────────────────────────────────────────────

    def place_twap(
        self,
        symbol: str,
        qty: int,
        duration_minutes: float,
        stop_loss: float,
        take_profit: float,
        n_slices: Optional[int] = None,
    ) -> AlgoHandle:
        """
        Time-Weighted Average Price order.

        Splits qty into n_slices equal child market orders evenly spaced over
        duration_minutes. Default n_slices comes from Config.TWAP_SLICES.
        """
        from config import Config
        n = n_slices or getattr(Config, "TWAP_SLICES", 5)
        n = max(1, min(n, qty))  # can't have more slices than shares
        handle = self._new_handle(symbol, qty, OrderTypeEnum.TWAP)
        t = threading.Thread(
            target=self._run_twap,
            args=(handle, symbol, qty, duration_minutes, stop_loss, take_profit, n),
            daemon=True,
            name=f"twap-{handle.algo_id[:8]}",
        )
        t.start()
        logger.info(
            f"[TWAP] Started {handle.algo_id[:8]} {symbol} qty={qty} "
            f"duration={duration_minutes}min slices={n}"
        )
        return handle

    def place_vwap(
        self,
        symbol: str,
        qty: int,
        duration_minutes: float,
        stop_loss: float,
        take_profit: float,
        n_slices: Optional[int] = None,
    ) -> AlgoHandle:
        """
        Volume-Weighted Average Price order.

        Distributes qty across n_slices according to the intraday volume
        profile (heavy at open/close, light at midday).
        """
        from config import Config
        n = n_slices or getattr(Config, "TWAP_SLICES", 5)
        n = max(1, min(n, qty))
        handle = self._new_handle(symbol, qty, OrderTypeEnum.VWAP)
        t = threading.Thread(
            target=self._run_vwap,
            args=(handle, symbol, qty, duration_minutes, stop_loss, take_profit, n),
            daemon=True,
            name=f"vwap-{handle.algo_id[:8]}",
        )
        t.start()
        logger.info(
            f"[VWAP] Started {handle.algo_id[:8]} {symbol} qty={qty} "
            f"duration={duration_minutes}min slices={n}"
        )
        return handle

    def place_iceberg(
        self,
        symbol: str,
        total_qty: int,
        display_qty: Optional[int],
        stop_loss: float,
        take_profit: float,
    ) -> AlgoHandle:
        """
        Iceberg order: show only display_qty at a time, refill until total_qty filled.

        display_qty defaults to Config.ICEBERG_DISPLAY_PCT × total_qty.
        """
        from config import Config
        pct = getattr(Config, "ICEBERG_DISPLAY_PCT", 0.20)
        d_qty = display_qty or max(1, int(total_qty * pct))
        d_qty = min(d_qty, total_qty)
        handle = self._new_handle(symbol, total_qty, OrderTypeEnum.ICEBERG)
        t = threading.Thread(
            target=self._run_iceberg,
            args=(handle, symbol, total_qty, d_qty, stop_loss, take_profit),
            daemon=True,
            name=f"iceberg-{handle.algo_id[:8]}",
        )
        t.start()
        logger.info(
            f"[ICEBERG] Started {handle.algo_id[:8]} {symbol} total={total_qty} display={d_qty}"
        )
        return handle

    def get_handle(self, algo_id: str) -> Optional[AlgoHandle]:
        with self._lock:
            return self._handles.get(algo_id)

    def list_active(self) -> List[AlgoHandle]:
        with self._lock:
            return [h for h in self._handles.values()
                    if h.status in (AlgoStatus.PENDING, AlgoStatus.RUNNING, AlgoStatus.PARTIAL)]

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _new_handle(self, symbol: str, qty: int, order_type: OrderTypeEnum) -> AlgoHandle:
        algo_id = str(uuid.uuid4())
        handle = AlgoHandle(algo_id=algo_id, symbol=symbol, total_qty=qty, order_type=order_type)
        with self._lock:
            self._handles[algo_id] = handle
        return handle

    def _submit_child(
        self,
        handle: AlgoHandle,
        symbol: str,
        qty: int,
        stop_loss: float,
        take_profit: float,
    ) -> Tuple[bool, float]:
        """Submit one child order. Returns (success, fill_price)."""
        price = self._get_price(symbol)
        if price is None:
            logger.warning(f"[{handle.order_type}] Cannot get price for {symbol}; skipping slice")
            return False, 0.0
        try:
            result = self._place(symbol, qty, price, stop_loss, take_profit)
            if result.success:
                handle.child_order_ids.append(result.order_id)
                # Update running average fill price
                prev_total = handle.avg_fill_price * handle.filled_qty
                handle.filled_qty += qty
                handle.avg_fill_price = (prev_total + result.entry_price * qty) / handle.filled_qty
                handle.status = AlgoStatus.PARTIAL
                return True, result.entry_price
            else:
                logger.warning(f"[{handle.order_type}] Child order failed for {symbol}: {result.message}")
                return False, 0.0
        except Exception as exc:
            logger.error(f"[{handle.order_type}] Exception on child order {symbol}: {exc}")
            return False, 0.0

    def _finalize(self, handle: AlgoHandle, succeeded: bool) -> None:
        handle.completed_at = datetime.now(timezone.utc)
        if handle.is_cancel_requested():
            handle.status = AlgoStatus.CANCELLED
        elif succeeded and handle.filled_qty >= handle.total_qty:
            handle.status = AlgoStatus.COMPLETE
        elif handle.filled_qty > 0:
            handle.status = AlgoStatus.PARTIAL
        else:
            handle.status = AlgoStatus.FAILED
        logger.info(
            f"[{handle.order_type}] {handle.algo_id[:8]} {handle.symbol} "
            f"→ {handle.status.value} filled={handle.filled_qty}/{handle.total_qty} "
            f"avg={handle.avg_fill_price:.4f}"
        )

    # ── Algo runners (each in its own thread) ─────────────────────────────

    def _run_twap(
        self,
        handle: AlgoHandle,
        symbol: str,
        qty: int,
        duration_minutes: float,
        stop_loss: float,
        take_profit: float,
        n_slices: int,
    ) -> None:
        handle.status = AlgoStatus.RUNNING
        slice_qty_base = qty // n_slices
        remainder = qty - slice_qty_base * n_slices
        interval_s = (duration_minutes * 60) / n_slices
        succeeded = True

        for i in range(n_slices):
            if handle.is_cancel_requested():
                break
            slice_qty = slice_qty_base + (1 if i < remainder else 0)
            if slice_qty <= 0:
                continue
            ok, _ = self._submit_child(handle, symbol, slice_qty, stop_loss, take_profit)
            if not ok:
                succeeded = False
            # Sleep until next slice (but wake early if cancelled)
            handle._cancel_event.wait(timeout=interval_s)

        self._finalize(handle, succeeded)

    def _run_vwap(
        self,
        handle: AlgoHandle,
        symbol: str,
        qty: int,
        duration_minutes: float,
        stop_loss: float,
        take_profit: float,
        n_slices: int,
    ) -> None:
        handle.status = AlgoStatus.RUNNING
        fractions = _vwap_slice_fractions(n_slices)
        interval_s = (duration_minutes * 60) / n_slices
        succeeded = True
        remaining = qty

        for i, frac in enumerate(fractions):
            if handle.is_cancel_requested():
                break
            # Last slice takes all remaining to avoid rounding loss
            if i == n_slices - 1:
                slice_qty = remaining
            else:
                slice_qty = max(1, round(qty * frac))
                slice_qty = min(slice_qty, remaining)
            remaining -= slice_qty
            if slice_qty <= 0:
                continue

            ok, _ = self._submit_child(handle, symbol, slice_qty, stop_loss, take_profit)
            if not ok:
                succeeded = False

            handle._cancel_event.wait(timeout=interval_s)

        self._finalize(handle, succeeded)

    def _run_iceberg(
        self,
        handle: AlgoHandle,
        symbol: str,
        total_qty: int,
        display_qty: int,
        stop_loss: float,
        take_profit: float,
    ) -> None:
        handle.status = AlgoStatus.RUNNING
        remaining = total_qty
        succeeded = True
        # Pause between tranches: ~2 seconds (configurable later)
        TRANCHE_PAUSE_S = 2.0

        while remaining > 0 and not handle.is_cancel_requested():
            tranche = min(display_qty, remaining)
            ok, _ = self._submit_child(handle, symbol, tranche, stop_loss, take_profit)
            if not ok:
                succeeded = False
                break
            remaining -= tranche
            if remaining > 0:
                handle._cancel_event.wait(timeout=TRANCHE_PAUSE_S)

        self._finalize(handle, succeeded)
