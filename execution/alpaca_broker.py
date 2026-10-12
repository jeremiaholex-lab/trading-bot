import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from config import Config

logger = logging.getLogger(__name__)

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

class AlpacaBroker:
    """
    Unified broker client for Alpaca (Live/Paper) with zero-config Local Simulator fallback.
    """

    def __init__(self):
        self.client = None
        self.is_simulated = True

        if Config.has_alpaca_credentials():
            try:
                from alpaca.trading.client import TradingClient
                self.client = TradingClient(
                    api_key=Config.ALPACA_API_KEY,
                    secret_key=Config.ALPACA_SECRET_KEY,
                    paper=not Config.ALPACA_LIVE
                )
                account = self.client.get_account()
                self.is_simulated = False
                mode_str = "LIVE" if Config.ALPACA_LIVE else "ALPACA_PAPER"
                logger.info(f"Connected to Alpaca ({mode_str}). Account #{account.account_number}, Equity: ${float(account.equity):,.2f}")
            except Exception as e:
                logger.warning(f"Failed to connect to Alpaca API: {e}. Switching to Local Paper Simulator mode.")
                self.is_simulated = True
        else:
            logger.info("No Alpaca API keys found in .env. Running in Safe Local Paper Simulator mode ($100,000 virtual balance).")
            self.is_simulated = True

        # State for local simulator
        self.sim_cash = 100000.0
        self.sim_positions: Dict[str, SimulatedPosition] = {}
        self.order_counter = 1

    def get_account_summary(self) -> Dict[str, Any]:
        """Returns account equity, cash, and active mode."""
        if not self.is_simulated and self.client:
            try:
                acc = self.client.get_account()
                return {
                    "equity": float(acc.equity),
                    "cash": float(acc.cash),
                    "buying_power": float(acc.buying_power),
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
            "mode": "LOCAL PAPER SIMULATOR (SAFE)",
            "currency": "USD"
        }

    def get_open_positions(self) -> Dict[str, Dict[str, Any]]:
        """Returns dictionary of active open positions keyed by symbol."""
        if not self.is_simulated and self.client:
            try:
                positions = self.client.get_all_positions()
                result = {}
                for p in positions:
                    result[p.symbol] = {
                        "symbol": p.symbol,
                        "qty": int(p.qty),
                        "entry_price": float(p.avg_entry_price),
                        "current_price": float(p.current_price),
                        "unrealized_pl": float(p.unrealized_pl),
                        "unrealized_plpc": float(p.unrealized_plpc) * 100
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
                from alpaca.trading.enums import OrderSide, TimeInForce

                req = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.GTC,
                    take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2)),
                    stop_loss=StopLossRequest(stop_price=round(stop_loss_price, 2))
                )
                order = self.client.submit_order(req)
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

    def update_simulated_prices(self, price_map: Dict[str, float]) -> List[str]:
        """
        In simulation mode, monitors price movements against stop loss and take profit brackets.
        Returns list of executed exit notices.
        """
        if not self.is_simulated:
            return []

        exits = []
        to_remove = []

        for sym, pos in self.sim_positions.items():
            if sym in price_map:
                curr_price = price_map[sym]
                pos.current_price = curr_price

                # Check Stop Loss
                if curr_price <= pos.stop_loss:
                    pl = (pos.stop_loss - pos.entry_price) * pos.qty
                    self.sim_cash += pos.qty * pos.stop_loss
                    exits.append(f"🔴 STOP LOSS TRIGGERED for {sym}: Sold {pos.qty} shares at ${pos.stop_loss:.2f} (P&L: ${pl:+,.2f})")
                    to_remove.append(sym)

                # Check Take Profit
                elif curr_price >= pos.take_profit:
                    pl = (pos.take_profit - pos.entry_price) * pos.qty
                    self.sim_cash += pos.qty * pos.take_profit
                    exits.append(f"🟢 TAKE PROFIT HIT for {sym}: Sold {pos.qty} shares at ${pos.take_profit:.2f} (P&L: ${pl:+,.2f})")
                    to_remove.append(sym)

        for sym in to_remove:
            del self.sim_positions[sym]

        return exits
