import sqlite3
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List
from config import Config

logger = logging.getLogger(__name__)

class TradeJournal:
    """
    Lightweight, persistent SQLite trade ledger and performance analyzer.
    Tracks all fills, partial scale-outs, realized P&L, hold durations,
    and Maximum Adverse/Favorable Excursions (MAE/MFE).
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or Config.TRADES_DB_PATH
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Initializes tables and indexes atomically."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT UNIQUE,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    stop_loss REAL,
                    take_profit REAL,
                    entry_time TEXT NOT NULL,
                    exit_time TEXT,
                    realized_pnl REAL DEFAULT 0.0,
                    realized_pnl_pct REAL DEFAULT 0.0,
                    holding_period_seconds REAL DEFAULT 0.0,
                    mae REAL DEFAULT 0.0,
                    mfe REAL DEFAULT 0.0,
                    status TEXT NOT NULL,
                    reason TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
            conn.commit()

    def record_entry(
        self,
        symbol: str,
        qty: int,
        entry_price: float,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
        reason: str = "",
        trade_id: Optional[str] = None
    ) -> str:
        """Records a new open trade entry."""
        now_iso = datetime.now(timezone.utc).isoformat()
        tid = trade_id or f"TRD-{symbol}-{uuid.uuid4().hex[:12]}"

        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO trades (
                    trade_id, symbol, side, qty, entry_price, stop_loss,
                    take_profit, entry_time, status, reason, mae, mfe
                ) VALUES (?, ?, 'BUY', ?, ?, ?, ?, ?, 'OPEN', ?, 0.0, 0.0)
            """, (tid, symbol, qty, round(entry_price, 2), round(stop_loss, 2),
                  round(take_profit, 2), now_iso, reason))
            conn.commit()

        logger.info(f"Journal: Recorded BUY entry {tid} for {qty} shares of {symbol} @ ${entry_price:.2f}")
        return tid

    def update_price_excursions(self, symbol: str, current_price: float):
        """
        Updates Maximum Adverse Excursion (MAE) and Maximum Favorable Excursion (MFE)
        while the position is open.
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT id, entry_price, mae, mfe FROM trades WHERE symbol = ? AND status IN ('OPEN', 'PARTIALLY_CLOSED')",
                (symbol,)
            ).fetchall()

            for row in rows:
                tid = row["id"]
                entry = float(row["entry_price"])
                current_pnl_per_share = current_price - entry

                current_mae = float(row["mae"])
                current_mfe = float(row["mfe"])

                new_mae = min(current_mae, current_pnl_per_share) if current_pnl_per_share < 0 else current_mae
                new_mfe = max(current_mfe, current_pnl_per_share) if current_pnl_per_share > 0 else current_mfe

                if new_mae != current_mae or new_mfe != current_mfe:
                    conn.execute(
                        "UPDATE trades SET mae = ?, mfe = ? WHERE id = ?",
                        (round(new_mae, 2), round(new_mfe, 2), tid)
                    )
            conn.commit()

    def record_partial_exit(
        self,
        symbol: str,
        qty: int,
        exit_price: float,
        reason: str = ""
    ) -> Optional[Dict[str, Any]]:
        """
        Records a partial scale-out. Books realized P&L on the closed portion,
        adjusts the remaining quantity on the parent trade, and records the exit event.
        """
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM trades WHERE symbol = ? AND status IN ('OPEN', 'PARTIALLY_CLOSED') ORDER BY id ASC LIMIT 1",
                (symbol,)
            ).fetchone()

            if not row:
                logger.warning(f"Journal: No open trade found for partial exit on {symbol}")
                return None

            trade_id = row["id"]
            entry_price = float(row["entry_price"])
            remaining_qty = int(row["qty"])
            close_qty = min(qty, remaining_qty)

            realized_pnl = (exit_price - entry_price) * close_qty
            realized_pct = ((exit_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0

            entry_time = datetime.fromisoformat(row["entry_time"])
            duration_sec = (now - entry_time).total_seconds()

            new_qty = remaining_qty - close_qty
            new_status = "CLOSED" if new_qty <= 0 else "PARTIALLY_CLOSED"

            accum_pnl = float(row["realized_pnl"]) + realized_pnl

            conn.execute("""
                UPDATE trades
                SET qty = ?, status = ?, exit_price = ?, exit_time = ?,
                    realized_pnl = ?, realized_pnl_pct = ?, holding_period_seconds = ?,
                    reason = CASE WHEN reason IS NULL OR reason = '' THEN ? ELSE reason || ' | ' || ? END
                WHERE id = ?
            """, (new_qty, new_status, round(exit_price, 2), now_iso,
                  round(accum_pnl, 2), round(realized_pct, 2), round(duration_sec, 1),
                  reason, reason, trade_id))
            conn.commit()

            logger.info(
                f"Journal: Partial exit on {symbol} ({close_qty} shares @ ${exit_price:.2f}). "
                f"Realized P&L: ${realized_pnl:+,.2f} ({realized_pct:+.2f}%)"
            )
            return {
                "symbol": symbol,
                "closed_qty": close_qty,
                "remaining_qty": new_qty,
                "exit_price": exit_price,
                "realized_pnl": realized_pnl,
                "realized_pct": realized_pct,
            }

    def record_full_exit(
        self,
        symbol: str,
        exit_price: float,
        reason: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Records a full trade closure at market or via bracket order."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM trades WHERE symbol = ? AND status IN ('OPEN', 'PARTIALLY_CLOSED') ORDER BY id ASC LIMIT 1",
                (symbol,)
            ).fetchone()

            if not row:
                logger.warning(f"Journal: No open trade found to close for {symbol}")
                return None

            trade_id = row["id"]
            entry_price = float(row["entry_price"])
            qty = int(row["qty"])

            portion_pnl = (exit_price - entry_price) * qty
            accum_pnl = float(row["realized_pnl"]) + portion_pnl
            realized_pct = ((exit_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0

            entry_time = datetime.fromisoformat(row["entry_time"])
            duration_sec = (now - entry_time).total_seconds()

            conn.execute("""
                UPDATE trades
                SET status = 'CLOSED', exit_price = ?, exit_time = ?,
                    realized_pnl = ?, realized_pnl_pct = ?, holding_period_seconds = ?,
                    reason = CASE WHEN reason IS NULL OR reason = '' THEN ? ELSE reason || ' | ' || ? END
                WHERE id = ?
            """, (round(exit_price, 2), now_iso, round(accum_pnl, 2),
                  round(realized_pct, 2), round(duration_sec, 1), reason, reason, trade_id))
            conn.commit()

            logger.info(
                f"Journal: Closed trade for {symbol} ({qty} shares @ ${exit_price:.2f}). "
                f"Total P&L: ${accum_pnl:+,.2f} ({realized_pct:+.2f}%)"
            )
            return {
                "symbol": symbol,
                "qty": qty,
                "exit_price": exit_price,
                "total_pnl": accum_pnl,
                "realized_pct": realized_pct,
                "duration_seconds": duration_sec,
            }

    def get_performance_summary(self) -> Dict[str, Any]:
        """Calculates institutional lifetime performance metrics across all trades."""
        with self._get_connection() as conn:
            trades = conn.execute(
                "SELECT * FROM trades WHERE status = 'CLOSED' OR realized_pnl != 0.0"
            ).fetchall()

        if not trades:
            return {
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate": 0.0,
                "total_realized_pnl": 0.0,
                "profit_factor": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
                "avg_trade_pnl": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "largest_win": 0.0,
                "largest_loss": 0.0,
                "avg_holding_hours": 0.0,
            }

        pnls = [float(t["realized_pnl"]) for t in trades]
        durations = [float(t["holding_period_seconds"]) for t in trades if t["holding_period_seconds"] > 0]

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        total_trades = len(pnls)
        winning_trades = len(wins)
        losing_trades = len(losses)
        win_rate = (winning_trades / total_trades * 100.0) if total_trades > 0 else 0.0

        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

        total_realized_pnl = sum(pnls)
        avg_trade_pnl = total_realized_pnl / total_trades if total_trades > 0 else 0.0
        avg_win = (sum(wins) / len(wins)) if wins else 0.0
        avg_loss = (sum(losses) / len(losses)) if losses else 0.0
        largest_win = max(wins) if wins else 0.0
        largest_loss = min(losses) if losses else 0.0
        avg_holding_hours = (sum(durations) / len(durations) / 3600.0) if durations else 0.0

        return {
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "win_rate": round(win_rate, 2),
            "total_realized_pnl": round(total_realized_pnl, 2),
            "profit_factor": round(profit_factor, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "avg_trade_pnl": round(avg_trade_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "largest_win": round(largest_win, 2),
            "largest_loss": round(largest_loss, 2),
            "avg_holding_hours": round(avg_holding_hours, 2),
        }

    def sync_broker_filled_orders(self, broker: Any) -> int:
        """
        Reconciles and backfills closed or filled trades that executed on the broker
        while the bot process was offline (e.g. overnight stops or target limits).
        """
        synced_count = 0
        if broker is None:
            return 0

        try:
            # Check for simulated broker exit details first
            sim_exits = getattr(broker, "last_simulated_exit_details", [])
            for exit_info in sim_exits:
                sym = exit_info.get("symbol")
                price = float(exit_info.get("exit_price", 0.0))
                reason = exit_info.get("reason", "Simulated exit while running/restarting")
                res = self.record_full_exit(sym, price, reason)
                if res:
                    synced_count += 1

            # In Alpaca live/paper mode, inspect filled closed orders
            client = getattr(broker, "client", None)
            if client and not getattr(broker, "is_simulated", True):
                from alpaca.trading.requests import GetOrdersRequest
                from alpaca.trading.enums import QueryOrderStatus

                req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=50)
                closed_orders = client.get_orders(req) or []

                with self._get_connection() as conn:
                    open_trades = conn.execute(
                        "SELECT symbol FROM trades WHERE status IN ('OPEN', 'PARTIALLY_CLOSED')"
                    ).fetchall()
                    open_symbols = {r["symbol"] for r in open_trades}

                # Find any open positions currently reported by the broker
                current_positions = broker.get_open_positions()

                for order in closed_orders:
                    sym = getattr(order, "symbol", "")
                    side = str(getattr(order, "side", "")).lower()
                    filled_qty = int(getattr(order, "filled_qty", 0) or 0)
                    filled_avg_price = float(getattr(order, "filled_avg_price", 0.0) or 0.0)

                    # If an order sold shares of an open symbol that is no longer held, record exit
                    if sym in open_symbols and "sell" in side and filled_qty > 0 and sym not in current_positions:
                        self.record_full_exit(
                            symbol=sym,
                            exit_price=filled_avg_price,
                            reason=f"Offline sync: closed by Alpaca filled order {getattr(order, 'id', '')}"
                        )
                        open_symbols.discard(sym)
                        synced_count += 1
                        logger.info(f"Journal synced offline exit for {sym} at ${filled_avg_price:.2f}")

        except Exception as e:
            logger.warning(f"Error syncing broker filled orders to trade journal: {e}")

        return synced_count

    def get_recent_trades(self, limit: int = 15) -> List[Dict[str, Any]]:
        """Returns the most recent trades for display."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
