import json
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable, TypeVar, Tuple
from config import Config

logger = logging.getLogger(__name__)

T = TypeVar("T")

def retry_api_call(
    func: Callable[[], T],
    max_retries: int = 3,
    base_delay: float = 1.0,
    backoff_factor: float = 2.0,
    operation_name: str = "API Call"
) -> T:
    """
    Executes a callable with exponential backoff retry logic.
    Retries on transient network errors and unexpected HTTP issues.
    """
    last_err: Optional[Exception] = None
    delay = base_delay
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            # Do not retry fatal auth or validation rejections
            if "insufficient" in err_str or "unauthorized" in err_str or "forbidden" in err_str:
                raise e
            if attempt < max_retries:
                logger.warning(
                    f"Transient failure during {operation_name} (attempt {attempt}/{max_retries}): {e}. "
                    f"Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
                delay *= backoff_factor
            else:
                logger.error(f"Permanent failure during {operation_name} after {max_retries} attempts: {e}")
    if last_err is not None:
        raise last_err
    raise RuntimeError(f"Unexpected termination in retry_api_call for {operation_name}")

@dataclass
class SimulatedPosition:
    symbol: str
    qty: int
    entry_price: float
    current_price: float
    stop_loss: float
    take_profit: float
    entry_time: str
    unrealized_pl: float = 0.0
    unrealized_plpc: float = 0.0

@dataclass
class OrderResult:
    success: bool
    order_id: str
    symbol: str
    qty: int
    entry_price: float
    stop_loss: float
    take_profit: float
    message: str


class BrokerAuthenticationError(RuntimeError):
    """Raised when Alpaca rejects the configured credentials.

    This is deliberately fatal. Falling back to the local simulator on an
    authentication failure makes the bot *look* operational while placing no
    orders at all, which is indistinguishable from a healthy trading day.
    """


class AlpacaBroker:
    """
    Unified broker client for Alpaca (Live/Paper) with zero-config Local Simulator fallback.
    """

    def __init__(self, force_simulator: bool = False, state_file: Optional[Path] = None):
        self.client = None
        self.is_simulated = True

        if force_simulator:
            logger.info("Forced into Local Paper Simulator mode (force_simulator=True).")
        elif Config.has_alpaca_credentials():
            self._connect_to_alpaca()
        else:
            logger.info("No Alpaca API keys found in .env. Running in Safe Local Paper Simulator mode ($100,000 virtual balance).")

        # State for local simulator
        self.sim_cash = 100000.0
        self.sim_positions: Dict[str, SimulatedPosition] = {}
        self.order_counter = 1
        # Equity at the previous session's close. For the simulator this
        # is simply the starting balance until a real session close is
        # recorded, so daily P&L reads as zero rather than as a fabricated
        # number.
        self.sim_last_equity = 100000.0
        self.last_simulated_exit_details: List[Dict[str, Any]] = []

        # The simulator's state is persisted so a restart does not silently
        # wipe open positions. Previously cash and positions lived only in
        # memory, so every restart reset the account to $100,000 while the
        # bot kept its tuned strategy parameters.
        self.state_file = state_file or (Config.BASE_DIR / "simulator_state.json")
        self._load_sim_state()

    def _load_sim_state(self):
        """Restores the simulator's cash and positions from disk.

        A corrupt or unreadable file is never fatal -- the simulator simply
        starts fresh, which is the safe default.
        """
        # Only the local simulator has a portfolio to restore. In live mode
        # the account lives on Alpaca's servers.
        if not self.is_simulated:
            return
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.sim_cash = float(data.get("cash", 100000.0))
            self.order_counter = int(data.get("order_counter", 1))
            positions = {}
            for sym, p in (data.get("positions") or {}).items():
                positions[sym] = SimulatedPosition(
                    symbol=p["symbol"],
                    qty=int(p["qty"]),
                    entry_price=float(p["entry_price"]),
                    current_price=float(p["current_price"]),
                    stop_loss=float(p["stop_loss"]),
                    take_profit=float(p["take_profit"]),
                    entry_time=p.get("entry_time", ""),
                )
            self.sim_positions = positions
            logger.info(
                f"Restored simulator state: ${self.sim_cash:,.2f}, {len(self.sim_positions)} open position(s)."
            )
        except Exception as e:
            logger.warning(
                f"Could not read simulator state from {self.state_file}: {e}. Starting fresh."
            )

    def _save_sim_state(self):
        """Atomically persists the simulator's cash and positions."""
        try:
            payload = {
                "cash": self.sim_cash,
                "order_counter": self.order_counter,
                "positions": {
                    sym: {
                        "symbol": p.symbol,
                        "qty": p.qty,
                        "entry_price": p.entry_price,
                        "current_price": p.current_price,
                        "stop_loss": p.stop_loss,
                        "take_profit": p.take_profit,
                        "entry_time": p.entry_time,
                    }
                    for sym, p in self.sim_positions.items()
                },
            }
            tmp = self.state_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            tmp.replace(self.state_file)
        except Exception as e:
            logger.error(f"Failed to save simulator state: {e}")

    def _connect_to_alpaca(self):
        """Authenticates with Alpaca.

        Raises BrokerAuthenticationError if the credentials are rejected. The
        only way to proceed in that case is to opt in to the simulator
        explicitly via ALLOW_SIMULATOR_FALLBACK=True.
        """
        from alpaca.trading.client import TradingClient

        try:
            self.client = TradingClient(
                api_key=Config.ALPACA_API_KEY,
                secret_key=Config.ALPACA_SECRET_KEY,
                paper=not Config.ALPACA_LIVE
            )
            account = retry_api_call(
                lambda: self.client.get_account(),
                max_retries=2,
                base_delay=1.0,
                operation_name="Alpaca Account Authentication"
            )
        except Exception as e:
            if Config.ALLOW_SIMULATOR_FALLBACK:
                logger.warning(
                    f"Failed to connect to Alpaca API: {e}. "
                    f"ALLOW_SIMULATOR_FALLBACK is enabled, switching to Local Paper Simulator mode."
                )
                self.is_simulated = True
                return

            raise BrokerAuthenticationError(
                f"Alpaca rejected the configured credentials: {e}. "
                f"The bot will NOT silently fall back to the local simulator, because that "
                f"trades nothing while appearing to trade. Fix the keys in .env "
                f"(run: python set_alpaca_keys.py), or set ALLOW_SIMULATOR_FALLBACK=True "
                f"to deliberately run on the simulator instead."
            ) from e

        self.is_simulated = False
        mode_str = "LIVE" if Config.ALPACA_LIVE else "ALPACA_PAPER"
        logger.info(f"Connected to Alpaca ({mode_str}). Account #{account.account_number}, Equity: ${float(account.equity):,.2f}")

    def get_latest_quote(self, symbol: str) -> Tuple[float, float]:
        """
        Retrieves current real-time bid and ask quotes for a symbol.
        Returns (bid_price, ask_price).
        """
        if not self.is_simulated and self.client:
            try:
                from alpaca.data.historical import StockHistoricalDataClient
                from alpaca.data.requests import StockLatestQuoteRequest

                data_client = StockHistoricalDataClient(
                    api_key=Config.ALPACA_API_KEY,
                    secret_key=Config.ALPACA_SECRET_KEY
                )
                req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
                quotes = data_client.get_stock_latest_quote(req)
                if symbol in quotes:
                    q = quotes[symbol]
                    return float(q.bid_price or 0.0), float(q.ask_price or 0.0)
            except Exception as e:
                logger.debug(f"Could not fetch Alpaca live quote for {symbol}: {e}")

        # Simulator fallback: tight simulated quote around last known price
        pos = self.sim_positions.get(symbol)
        ref_price = pos.current_price if pos else 100.0
        return round(ref_price * 0.9998, 2), round(ref_price * 1.0002, 2)

    def get_market_clock(self) -> Dict[str, Any]:
        """
        Returns market clock info: is_open, next_open, next_close.
        Uses Alpaca API when connected, otherwise falls back to US Eastern timezone schedule.
        """
        if not self.is_simulated and self.client:
            try:
                clock = retry_api_call(
                    lambda: self.client.get_clock(),
                    max_retries=2,
                    base_delay=1.0,
                    operation_name="get_clock"
                )
                return {
                    "is_open": clock.is_open,
                    "next_open": clock.next_open,
                    "next_close": clock.next_close,
                }
            except Exception as e:
                logger.warning(f"Failed to fetch Alpaca market clock: {e}. Using timezone fallback.")

        from zoneinfo import ZoneInfo
        return self._simulated_clock(datetime.now(ZoneInfo("America/New_York")))

    def _simulated_clock(self, et_now=None) -> Dict[str, Any]:
        """Timezone-based market clock used when Alpaca is not connected.

        Crucially, this always reports a `next_open` while the market is
        closed. It previously returned None after the close, which sent the
        trading loop into a 5-minute poll loop for the entire night and
        weekend instead of sleeping until the next session.
        """
        from zoneinfo import ZoneInfo

        if et_now is None:
            et_now = datetime.now(ZoneInfo("America/New_York"))

        weekday = et_now.weekday()  # Monday=0, Sunday=6

        # Market open Mon-Fri 9:30 AM - 4:00 PM ET
        market_open_time = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close_time = et_now.replace(hour=16, minute=0, second=0, microsecond=0)

        is_open = (0 <= weekday <= 4) and (market_open_time <= et_now <= market_close_time)

        if is_open:
            return {
                "is_open": True,
                "next_open": None,
                "next_close": market_close_time,
            }

        # Find the next weekday session.
        days_ahead = 1
        candidate = (et_now + timedelta(days=1)).replace(
            hour=9, minute=30, second=0, microsecond=0
        )
        while candidate.weekday() > 4:
            candidate += timedelta(days=1)

        # If it is a weekday but before the open, today's session is next.
        if et_now.weekday() <= 4 and et_now < market_open_time:
            candidate = market_open_time

        return {
            "is_open": False,
            "next_open": candidate,
            "next_close": None,
        }

    def get_account_summary(self) -> Dict[str, Any]:
        """Returns account equity, cash, and active mode."""
        if not self.is_simulated and self.client:
            try:
                acc = retry_api_call(
                    lambda: self.client.get_account(),
                    max_retries=3,
                    base_delay=1.0,
                    operation_name="get_account"
                )
                return {
                    "equity": float(acc.equity),
                    "cash": float(acc.cash),
                    "buying_power": float(acc.buying_power),
                    # Alpaca's last_equity is the equity at the previous
                    # session's close. It is the only correct denominator
                    # for "what did today make me" -- the bot's own
                    # start-of-day snapshot can drift if the process
                    # restarts mid-session.
                    "last_equity": float(getattr(acc, "last_equity", 0.0) or 0.0),
                    "mode": "LIVE REAL MONEY" if Config.ALPACA_LIVE else "ALPACA PAPER CLOUD",
                    "currency": acc.currency
                }
            except Exception as e:
                logger.error(f"Error fetching Alpaca account: {e}")

        # Local simulation account
        pos_value = sum(p.qty * p.current_price for p in self.sim_positions.values())
        equity = self.sim_cash + pos_value
        return {
            "equity": round(equity, 2),
            "cash": round(self.sim_cash, 2),
            "buying_power": round(self.sim_cash * 2, 2), # 2x leverage
            "last_equity": round(self.sim_last_equity or equity, 2),
            "mode": "LOCAL PAPER SIMULATOR (SAFE)",
            "currency": "USD"
        }

    def _get_protective_order_prices(self) -> tuple:
        """Reads the stop and take-profit prices from the open orders.

        Alpaca keeps the protective stop and take-profit as separate open
        orders. The position object does not carry their prices, so without
        this the trailing-stop logic would have no stop to compare against
        and would skip every live position.

        Returns (stop_prices, limit_prices) keyed by symbol.
        """
        stops: Dict[str, float] = {}
        limits: Dict[str, float] = {}

        if self.is_simulated or not self.client:
            return stops, limits

        try:
            from alpaca.trading.requests import GetOrdersRequest

            # status="all" because Alpaca's "open" filter hides HELD
            # orders, and a bracket stop-loss is HELD while the position
            # is open. Querying "open" here returned only the take-profit,
            # so the trailing-stop logic never saw the live stop price and
            # skipped every position.
            orders = self.client.get_orders(
                GetOrdersRequest(status="all", limit=500)
            )
            orders = [o for o in (orders or []) if self._is_live_order(o)]
        except Exception as e:
            logger.warning(f"Could not read working orders for protective prices: {e}")
            return stops, limits

        for order in orders or []:
            try:
                order_type = str(getattr(order, "type", "") or "").lower()
                symbol = order.symbol
                if "stop" in order_type and getattr(order, "stop_price", None):
                    stops[symbol] = float(order.stop_price)
                elif "limit" in order_type and getattr(order, "limit_price", None):
                    limits[symbol] = float(order.limit_price)
            except Exception:
                continue

        return stops, limits

    def get_open_positions(self) -> Dict[str, Dict[str, Any]]:
        """Returns dictionary of active open positions keyed by symbol."""
        if not self.is_simulated and self.client:
            try:
                positions = retry_api_call(
                    lambda: self.client.get_all_positions(),
                    max_retries=3,
                    base_delay=1.0,
                    operation_name="get_all_positions"
                )
                # The live stop price lives on the open stop order, not on the
                # position. Without it the trailing-stop logic has nothing to
                # compare against and silently skips every position.
                stops, limits = self._get_protective_order_prices()

                result = {}
                for p in positions:
                    result[p.symbol] = {
                        "symbol": p.symbol,
                        "qty": int(p.qty),
                        "entry_price": float(p.avg_entry_price),
                        "current_price": float(p.current_price),
                        "unrealized_pl": float(p.unrealized_pl),
                        "unrealized_plpc": float(p.unrealized_plpc) * 100,
                        "stop_loss": stops.get(p.symbol, 0.0),
                        "take_profit": limits.get(p.symbol, 0.0),
                    }
                return result
            except Exception as e:
                logger.error(f"Error fetching Alpaca positions: {e}")
                return {}

        # Local simulation positions
        res = {}
        for sym, p in self.sim_positions.items():
            pl = (p.current_price - p.entry_price) * p.qty
            plpc = ((p.current_price - p.entry_price) / p.entry_price) * 100 if p.entry_price > 0 else 0.0
            res[sym] = {
                "symbol": sym,
                "qty": p.qty,
                "entry_price": p.entry_price,
                "current_price": p.current_price,
                "stop_loss": p.stop_loss,
                "take_profit": p.take_profit,
                "unrealized_pl": round(pl, 2),
                "unrealized_plpc": round(plpc, 2)
            }
        return res

    def place_bracket_buy_order(
        self,
        symbol: str,
        qty: int,
        entry_price: float,
        stop_loss_price: float,
        take_profit_price: float
    ) -> OrderResult:
        """
        Submits a Market Buy with server-side Bracket Stop Loss & Take Profit.
        """
        if not self.is_simulated and self.client:
            try:
                from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
                from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

                # order_class=BRACKET is REQUIRED. Without it Alpaca accepts
                # the take_profit/stop_loss fields, returns a successful
                # order, and then silently creates NO protective legs. The
                # position ends up with a stop that does not exist and a
                # take-profit that does not exist, while every layer of the
                # bot reports a normal, healthy-looking position.
                req = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.GTC,
                    order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2)),
                    stop_loss=StopLossRequest(stop_price=round(stop_loss_price, 2))
                )
                order = retry_api_call(
                    lambda: self.client.submit_order(req),
                    max_retries=2,
                    base_delay=1.0,
                    operation_name=f"submit_order({symbol})"
                )
                # Verify the protective legs actually exist. A bracket
                # order can be accepted and still come back without legs,
                # which leaves the position with no stop and no target
                # while every status display says everything is fine.
                # Checking here means the caller learns immediately.
                time.sleep(1.0)
                has_stop = False
                has_limit = False
                for existing in self._get_symbol_orders(symbol):
                    order_type = str(getattr(existing, "type", "") or "").lower()
                    if "stop" in order_type:
                        has_stop = True
                    elif "limit" in order_type:
                        has_limit = True

                if not (has_stop and has_limit):
                    logger.critical(
                        f"BRACKET ORDER FOR {symbol} IS MISSING PROTECTIVE LEGS "
                        f"(stop={has_stop}, target={has_limit}). The position may be "
                        f"unprotected. Attempting to restore."
                    )
                    restored = self.ensure_protective_orders()
                    if symbol not in restored:
                        return OrderResult(
                            success=False,
                            order_id=str(order.id),
                            symbol=symbol,
                            qty=qty,
                            entry_price=entry_price,
                            stop_loss=stop_loss_price,
                            take_profit=take_profit_price,
                            message=(
                                "Order filled but the protective stop-loss/take-profit "
                                "legs were NOT created and could not be restored. "
                                "Treat this position as unprotected."
                            ),
                        )

                return OrderResult(
                    success=True,
                    order_id=str(order.id),
                    symbol=symbol,
                    qty=qty,
                    entry_price=entry_price,
                    stop_loss=stop_loss_price,
                    take_profit=take_profit_price,
                    message=f"Alpaca Bracket Order Submitted. Status: {order.status}"
                )
            except Exception as e:
                logger.error(f"Failed to submit Alpaca order: {e}")
                return OrderResult(
                    success=False,
                    order_id="",
                    symbol=symbol,
                    qty=qty,
                    entry_price=entry_price,
                    stop_loss=stop_loss_price,
                    take_profit=take_profit_price,
                    message=f"Order rejected: {e}"
                )

        # Local Simulator Execution
        cost = qty * entry_price
        if self.sim_cash < cost:
            return OrderResult(
                success=False,
                order_id="",
                symbol=symbol,
                qty=qty,
                entry_price=entry_price,
                stop_loss=stop_loss_price,
                take_profit=take_profit_price,
                message="Insufficient simulator cash."
            )

        self.sim_cash -= cost
        order_id = f"SIM-{self.order_counter}"
        self.order_counter += 1

        self.sim_positions[symbol] = SimulatedPosition(
            symbol=symbol,
            qty=qty,
            entry_price=entry_price,
            current_price=entry_price,
            stop_loss=stop_loss_price,
            take_profit=take_profit_price,
            entry_time=datetime.now(timezone.utc).isoformat()
        )
        self._save_sim_state()

        return OrderResult(
            success=True,
            order_id=order_id,
            symbol=symbol,
            qty=qty,
            entry_price=entry_price,
            stop_loss=stop_loss_price,
            take_profit=take_profit_price,
            message=f"Filled in Local Simulator at ${entry_price:.2f}"
        )

    def _cancel_symbol_orders(self, symbol: str, order_types: Optional[tuple] = None) -> List[str]:
        """
        Cancels open orders for a symbol and returns the ids cancelled.

        The shares of a position opened with a bracket order are *held* by
        the open stop-loss and take-profit orders. Alpaca rejects a sell
        with "insufficient qty available for order (requested: 13,
        available: 0)" unless those protective orders are cancelled first.

        `order_types` restricts the cancellation to matching order types
        (e.g. only stops). This matters when trailing a stop: cancelling
        every order would throw away the take-profit as well.

        Note: alpaca-py's TradingClient has no `cancel_open_orders`; the
        real surface is `get_orders` plus `cancel_order_by_id`.
        """
        cancelled = []
        try:
            orders = self._get_symbol_orders(symbol)
        except Exception as e:
            logger.warning(f"Could not list working orders for {symbol}: {e}")
            return cancelled

        for order in orders or []:
            try:
                order_type = str(getattr(order, "type", "") or "").lower()
                if order_types and not any(t in order_type for t in order_types):
                    continue
                self.client.cancel_order_by_id(order.id)
                cancelled.append(str(order.id))
            except Exception as e:
                logger.warning(f"Could not cancel order {order.id} for {symbol}: {e}")

        if cancelled:
            logger.info(f"Cancelled {len(cancelled)} open order(s) for {symbol}.")
        return cancelled

    def place_close_position_order(self, symbol: str, qty: int, reason: str = "") -> OrderResult:
        """
        Closes an open position at market.

        Used to act on a SELL signal. The bracket orders attached to the
        original entry hold the shares, so they are cancelled first.
        """
        if not self.is_simulated and self.client:
            try:
                from alpaca.trading.requests import MarketOrderRequest
                from alpaca.trading.enums import OrderSide, TimeInForce

                cancelled = self._cancel_symbol_orders(symbol)
                if cancelled:
                    time.sleep(0.8)

                req = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.GTC,
                )
                order = retry_api_call(
                    lambda: self.client.submit_order(req),
                    max_retries=2,
                    base_delay=1.0,
                    operation_name=f"close_position({symbol})"
                )
                return OrderResult(
                    success=True,
                    order_id=str(order.id),
                    symbol=symbol,
                    qty=qty,
                    entry_price=0.0,
                    stop_loss=0.0,
                    take_profit=0.0,
                    message=f"Close order submitted for {symbol}. Status: {order.status}"
                )
            except Exception as e:
                logger.error(f"Failed to submit Alpaca close order for {symbol}: {e}")
                return OrderResult(
                    success=False,
                    order_id="",
                    symbol=symbol,
                    qty=qty,
                    entry_price=0.0,
                    stop_loss=0.0,
                    take_profit=0.0,
                    message=f"Close order rejected: {e}"
                )

        # Local Simulator Execution
        position = self.sim_positions.get(symbol)
        if position is None:
            return OrderResult(
                success=False, order_id="", symbol=symbol, qty=qty,
                entry_price=0.0, stop_loss=0.0, take_profit=0.0,
                message=f"No open position for {symbol}.",
            )

        close_qty = min(qty, position.qty) if (qty is not None and qty > 0) else position.qty
        proceeds = close_qty * position.current_price
        self.sim_cash += proceeds
        pnl = (position.current_price - position.entry_price) * close_qty

        if close_qty >= position.qty:
            del self.sim_positions[symbol]
            action_desc = f"Closed {close_qty} shares"
        else:
            position.qty -= close_qty
            action_desc = f"Partially closed {close_qty} shares (remaining: {position.qty})"

        self._save_sim_state()

        order_id = f"SIM-CLOSE-{self.order_counter}"
        self.order_counter += 1

        return OrderResult(
            success=True,
            order_id=order_id,
            symbol=symbol,
            qty=close_qty,
            entry_price=position.entry_price,
            stop_loss=0.0,
            take_profit=0.0,
            message=f"{action_desc} of {symbol} at ${position.current_price:.2f} "
                    f"(P&L: ${pnl:+,.2f}){(' - ' + reason) if reason else ''}"
        )

    # Alpaca's `status=open` filter does NOT include orders in the HELD
    # status, and a stop-loss on a bracket position is exactly that: it is
    # held while the entry fills. Querying `open` therefore returns the
    # take-profit but NEVER the stop-loss.
    #
    # That made every protection check in this bot blind to stop-losses:
    # the trailing-stop logic could not read the current stop, the
    # protective-order audit believed positions had no protection, and it
    # then tried to "restore" a stop that was already there -- cancelling
    # the real one and leaving the position bare.
    #
    # Only `status=all` surfaces HELD orders. The set of orders considered
    # live is then filtered explicitly, since `all` also returns filled,
    # cancelled and expired orders.
    _LIVE_ORDER_STATUSES = {
        "new", "held", "accepted", "accepted_for_bidding", "calculated",
        "partially_filled", "pending_new", "pending_replace",
        "pending_cancel", "pending_review", "suspended", "stopped",
    }

    def _is_live_order(self, order: Any) -> bool:
        """True if the order is still working (not filled/cancelled/expired)."""
        status = str(getattr(order, "status", "") or "").lower()
        if not status:
            return True
        return status.split(".")[-1] in self._LIVE_ORDER_STATUSES

    def _get_symbol_orders(self, symbol: str) -> List[Any]:
        """Returns the WORKING orders for a symbol.

        Includes HELD stop-losses, which Alpaca's `open` filter hides.
        """
        try:
            from alpaca.trading.requests import GetOrdersRequest

            orders = self.client.get_orders(
                GetOrdersRequest(status="all", symbols=[symbol], limit=100)
            ) or []
            return [o for o in orders if self._is_live_order(o)]
        except Exception as e:
            logger.warning(f"Could not list working orders for {symbol}: {e}")
            return []

    def update_trailing_stop(self, symbol: str, new_stop: float) -> bool:
        """
        Moves the protective stop on an open position.

        In Alpaca mode the existing stop order is *replaced* (a PATCH on
        the order), not cancelled and resubmitted. The old approach
        cancelled first and submitted second, which left a window where
        the position had NO protective stop at all. Worse, Alpaca rejects
        the replacement submit with "insufficient qty available" while
        the cancelled order's shares are still held -- so the position
        stayed permanently unprotected. That is the worst possible failure
        for a position that is being trailed because it is in profit.

        In simulator mode the stop recorded on the position is updated.
        """
        if not self.is_simulated and self.client:
            try:
                from alpaca.trading.requests import StopOrderRequest, ReplaceOrderRequest
                from alpaca.trading.enums import OrderSide, TimeInForce

                position = self.get_open_positions().get(symbol)
                qty = int(position.get("qty", 0)) if position else 0
                if qty <= 0:
                    logger.warning(f"Cannot trail {symbol}: no open position.")
                    return False

                # Find the existing protective stop and replace it atomically.
                stop_order = None
                for order in self._get_symbol_orders(symbol):
                    order_type = str(getattr(order, "type", "") or "").lower()
                    if "stop" in order_type:
                        stop_order = order
                        break

                if stop_order is not None:
                    replace_req = ReplaceOrderRequest(
                        qty=qty,
                        time_in_force=TimeInForce.GTC,
                        stop_price=round(new_stop, 2),
                    )
                    self.client.replace_order_by_id(stop_order.id, replace_req)
                    logger.info(
                        f"Trailing stop for {symbol} moved to ${new_stop:.2f} "
                        f"(replaced order {stop_order.id})"
                    )
                    return True

                # No stop order exists (a previous run may have removed it).
                # Submit a fresh one so the position is never left bare.
                req = StopOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    type="stop",
                    time_in_force=TimeInForce.GTC,
                    stop_price=round(new_stop, 2),
                )
                order = self.client.submit_order(req)
                logger.info(
                    f"Restored missing trailing stop for {symbol} at ${new_stop:.2f} "
                    f"(order {order.id})"
                )
                return True
            except Exception as e:
                logger.error(f"Failed to update trailing stop for {symbol}: {e}")
                return False

        position = self.sim_positions.get(symbol)
        if position is None:
            return False
        # A trailing stop must never loosen.
        if new_stop <= position.stop_loss:
            return False
        position.stop_loss = round(new_stop, 2)
        self._save_sim_state()
        return True

    def ensure_protective_orders(self) -> Dict[str, List[str]]:
        """
        Makes sure every open position still has a stop-loss and a
        take-profit.

        This is the safety net. A position with no protective orders can
        lose money without limit, and nothing else in the bot would notice:
        the position would still show up in the dashboard, still count
        toward MAX_OPEN_POSITIONS, and still report a plausible P&L.

        Both legs are submitted as a SINGLE OCO sell order. Two separate
        sell orders cannot both hold the same shares -- Alpaca reserves
        the quantity per order, so the second one is rejected with
        "insufficient qty available". One OCO order reserves the
        quantity once and carries both the stop and the target.

        Returns a dict of symbol -> list of problems found and fixed.
        """
        problems: Dict[str, List[str]] = {}
        if self.is_simulated or not self.client:
            return problems

        try:
            from alpaca.trading.requests import OrderRequest, TakeProfitRequest, StopLossRequest
            from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, OrderClass

            positions = self.get_open_positions()
            for symbol, position in positions.items():
                qty = int(position.get("qty", 0))
                if qty <= 0:
                    continue

                has_stop = False
                has_limit = False
                stale_orders = []
                for order in self._get_symbol_orders(symbol):
                    order_type = str(getattr(order, "type", "") or "").lower()
                    if "stop" in order_type:
                        has_stop = True
                        stale_orders.append(order)
                    elif "limit" in order_type:
                        has_limit = True
                        stale_orders.append(order)

                if has_stop and has_limit:
                    continue

                entry = float(position.get("entry_price", 0.0))
                current = float(position.get("current_price", entry))
                reference = entry or current
                stop_price = round(reference * 0.995, 2)
                limit_price = round(reference * 1.01, 2)

                # Cancel whatever partial protection exists first. It has
                # to go before the combined OCO can reserve the shares.
                for order in stale_orders:
                    try:
                        self.client.cancel_order_by_id(order.id)
                    except Exception as e:
                        logger.warning(
                            f"Could not cancel order {order.id} for {symbol}: {e}"
                        )

                try:
                    # order_class=OCO makes this an exit order for an
                    # existing position: one order reserving the shares
                    # once, carrying both the stop and the target.
                    #
                    # The `type` MUST be LIMIT for an OCO exit. With
                    # type=MARKET Alpaca ignores the legs entirely and
                    # fills the order immediately at market -- which
                    # closes the position instead of protecting it.
                    req = OrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=OrderSide.SELL,
                        type=OrderType.LIMIT,
                        time_in_force=TimeInForce.GTC,
                        order_class=OrderClass.OCO,
                        take_profit=TakeProfitRequest(limit_price=limit_price),
                        stop_loss=StopLossRequest(stop_price=stop_price),
                    )
                    self.client.submit_order(req)
                    missing = []
                    if not has_stop:
                        missing.append(f"stop-loss at ${stop_price:,.2f}")
                    if not has_limit:
                        missing.append(f"take-profit at ${limit_price:,.2f}")
                    problems.setdefault(symbol, []).append(
                        "Restored missing " + " and ".join(missing)
                    )
                    logger.warning(
                        f"POSITION {symbol} WAS MISSING PROTECTION. "
                        f"Restored {' and '.join(missing)}."
                    )
                except Exception as e:
                    logger.error(f"Could not restore protective orders for {symbol}: {e}")
                    problems.setdefault(symbol, []).append(
                        f"FAILED to restore protection: {e}"
                    )

        except Exception as e:
            logger.error(f"Could not verify protective orders: {e}")

        return problems

    def update_simulated_prices(self, price_map: Dict[str, float]) -> List[str]:
        """
        In simulation mode, monitors price movements against stop loss and take profit brackets.
        Returns list of executed exit notices.
        """
        if not self.is_simulated:
            return []

        exits = []
        to_remove = []
        exit_details = []

        for sym, pos in self.sim_positions.items():
            if sym in price_map:
                curr_price = price_map[sym]
                pos.current_price = curr_price

                # Check Stop Loss
                if curr_price <= pos.stop_loss:
                    pl = (pos.stop_loss - pos.entry_price) * pos.qty
                    self.sim_cash += pos.qty * pos.stop_loss
                    reason = f"Stop Loss triggered at ${pos.stop_loss:.2f}"
                    exits.append(f"🔴 STOP LOSS TRIGGERED for {sym}: Sold {pos.qty} shares at ${pos.stop_loss:.2f} (P&L: ${pl:+,.2f})")
                    exit_details.append({
                        "symbol": sym,
                        "qty": pos.qty,
                        "exit_price": pos.stop_loss,
                        "pnl": pl,
                        "reason": reason,
                    })
                    to_remove.append(sym)

                # Check Take Profit
                elif curr_price >= pos.take_profit:
                    pl = (pos.take_profit - pos.entry_price) * pos.qty
                    self.sim_cash += pos.qty * pos.take_profit
                    reason = f"Take Profit hit at ${pos.take_profit:.2f}"
                    exits.append(f"🟢 TAKE PROFIT HIT for {sym}: Sold {pos.qty} shares at ${pos.take_profit:.2f} (P&L: ${pl:+,.2f})")
                    exit_details.append({
                        "symbol": sym,
                        "qty": pos.qty,
                        "exit_price": pos.take_profit,
                        "pnl": pl,
                        "reason": reason,
                    })
                    to_remove.append(sym)

        for sym in to_remove:
            del self.sim_positions[sym]

        if to_remove:
            self._save_sim_state()

        self.last_simulated_exit_details = exit_details
        return exits
