import sys
import os
import json
import time
import argparse
import logging
from datetime import datetime, timezone
from typing import Dict, Any

import pandas as pd

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from config import Config
from data.market_data import MarketDataProvider
from strategy.adaptive_strategy import AdaptiveStrategy, TradeSignal
from strategy.scanner import AssetScanner
from strategy.macro_filter import MacroTrendFilter
from risk.risk_manager import RiskManager
from optimizer.self_improver import SelfImprover
from optimizer.backtester import StrategyBacktester
from execution.alpaca_broker import AlpacaBroker, OrderResult
from execution.webhook import WebhookNotifier
from execution.trade_journal import TradeJournal

# Safety infrastructure
from safety import KillSwitch, AuditLedger, MandateGate

# Configure UTF-8 encoding for Windows console compatibility
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(Config.LOGS_DIR / "bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
# Reduce noisy third-party loggers
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.WARNING)
logging.getLogger("peewee").setLevel(logging.WARNING)

logger = logging.getLogger("TradingBot")
console = Console(highlight=False)

class TradingBotOrchestrator:
    """Master orchestrator integrating data, strategy, risk, optimizer, and execution."""

    # --- Session reporting state -------------------------------------
    # Declared at class level with None sentinels so the attributes always
    # exist, even on an instance built without __init__ (the test suite
    # does this to avoid touching the network). _ensure_session_state()
    # rebinds them per instance, so the lists are never shared between
    # instances.
    _session_trades = None
    _session_warnings = None
    _session_scans = 0
    _was_market_open = False
    _report_sent_for_date = None

    def __init__(self):
        Config.assert_valid()
        logger.info("Using account profile %s", Config.BOT_PROFILE)
        self.market_data = MarketDataProvider()
        self.broker = AlpacaBroker()
        self.risk_mgr = RiskManager()
        self.self_improver = SelfImprover()
        self.notifier = WebhookNotifier()
        self.macro_filter = MacroTrendFilter()
        self._scaled_out_positions = set()
        self.broker.init_algo_manager(self.market_data)

        # Expose safety infrastructure from broker for risk manager and other components
        self.kill_switch = self.broker.kill_switch
        self.audit_ledger = self.broker.audit_ledger
        self.mandate_gate = self.broker.mandate_gate
        self.advisory_orchestrator = self.broker.advisory_orchestrator

        # Initialize start of day equity
        acc = self.broker.get_account_summary()
        self.risk_mgr.update_start_of_day_equity(acc['equity'])

        # Pass safety infrastructure to risk manager
        self.risk_mgr.kill_switch = self.kill_switch
        self.risk_mgr.audit_ledger = self.audit_ledger
        self.risk_mgr.mandate_gate = self.mandate_gate

        # --- Session reporting state -------------------------------------
        # The operator wants one report per trading session, not one per
        # scan. These track what happened during the current session so the
        # end-of-day report can say something useful instead of just a
        # P&L number.
        self._ensure_session_state()

        # --- Liveness heartbeat -----------------------------------------
        # The live loop writes this file on every cycle. A separate
        # `bot.py --status-telegram` process reads it so the status
        # message reflects the RUNNING bot rather than a newly spawned
        # one with an empty session.
        self._heartbeat_path = Config.HEARTBEAT_PATH
        self._cycle_count = 0
        self._market_open = False
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._next_open = None

    def _ensure_session_state(self):
        """Initialises the per-instance session tracking containers.

        Called from __init__ and defensively from the accessors, so an
        instance built without __init__ (the test suite does this to avoid
        touching the network) still works. The lists are per-instance, so
        two orchestrators never share state.
        """
        if getattr(self, "_session_trades", None) is None:
            self._session_trades = []
        if getattr(self, "_session_warnings", None) is None:
            self._session_warnings = []
        if getattr(self, "_session_scans", None) is None:
            self._session_scans = 0
        if getattr(self, "_scaled_out_positions", None) is None:
            self._scaled_out_positions = set()
        if getattr(self, "macro_filter", None) is None:
            self.macro_filter = MacroTrendFilter()


    @property
    def journal(self) -> TradeJournal:
        """Lazily created so orchestrators built without __init__ still work."""
        if getattr(self, "_journal", None) is None:
            self._journal = TradeJournal()
        return self._journal

    def print_dashboard_status(self):
        """Displays rich terminal dashboard of portfolio status and open trades."""
        acc = self.broker.get_account_summary()
        open_pos = self.broker.get_open_positions()

        # Account Panel
        pnl_color = "green" if acc['equity'] >= 100000.0 else "yellow"
        info_text = (
            f"[bold]Execution Mode:[/bold] [cyan]{acc['mode']}[/cyan]\n"
            f"[bold]Total Equity:[/bold] [{pnl_color}]${acc['equity']:,.2f}[/{pnl_color}]\n"
            f"[bold]Available Cash:[/bold] ${acc['cash']:,.2f}\n"
            f"[bold]Buying Power:[/bold] ${acc['buying_power']:,.2f}\n"
            f"[bold]Open Positions:[/bold] {len(open_pos)} / {Config.MAX_OPEN_POSITIONS}"
        )
        console.print(Panel(info_text, title="🤖 [bold blue]AI Trading Bot - Portfolio Overview[/bold blue]", expand=False))

        # Open Positions Table
        if open_pos:
            pos_table = Table(title="📊 Active Open Positions")
            pos_table.add_column("Symbol", style="bold cyan")
            pos_table.add_column("Qty", justify="right")
            pos_table.add_column("Entry Price", justify="right")
            pos_table.add_column("Current Price", justify="right")
            pos_table.add_column("Unrealized P&L ($)", justify="right")
            pos_table.add_column("Return (%)", justify="right")

            for sym, pos in open_pos.items():
                pl = pos['unrealized_pl']
                plpc = pos['unrealized_plpc']
                c = "green" if pl >= 0 else "red"
                pos_table.add_row(
                    sym,
                    str(pos['qty']),
                    f"${pos['entry_price']:.2f}",
                    f"${pos['current_price']:.2f}",
                    f"[{c}]${pl:+,.2f}[/{c}]",
                    f"[{c}]{plpc:+.2f}%[/{c}]"
                )
            console.print(pos_table)
        else:
            console.print("[dim]No active open positions. Scanning for setups...[/dim]\n")

    def _notify_order(self, side: str, symbol: str, qty: int, price: float, reason: str = ""):
        """Sends a trade notification. Never allowed to break the trading loop."""
        # Record it for the session report first. The report must include
        # every trade even if webhook is not configured or fails.
        self._ensure_session_state()
        self._session_trades.append({
            "side": side,
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "reason": reason,
        })
        notifier = getattr(self, "notifier", None)
        if notifier is None:
            return
        try:
            notifier.notify_order_execution(symbol, side, qty, price)
        except Exception as e:
            logger.warning(f"Could not send order notification for {symbol}: {e}")

    def _notify_circuit_breaker(self, reason: str, symbol: str = ""):
        """Sends a critical circuit-breaker notification."""
        notifier = getattr(self, "notifier", None)
        if notifier is None:
            return
        self._ensure_session_state()
        self._session_warnings.append(
            f"Circuit breaker tripped ({symbol}): {reason}" if symbol
            else f"Circuit breaker tripped: {reason}"
        )
        try:
            equity = self.broker.get_account_summary().get("equity", 0.0)
            start = getattr(self.risk_mgr, "start_of_day_equity", None) or equity
            drawdown = (start - equity) / start if start else 0.0
            notifier.notify_circuit_breaker(reason, drawdown)
        except Exception as e:
            logger.warning(f"Could not send circuit breaker notification: {e}")

    def _record_session_warning(self, warning: str):
        """Records something worth telling the operator in the session report."""
        self._ensure_session_state()
        if warning and warning not in self._session_warnings:
            self._session_warnings.append(warning)

    def _build_session_report(self) -> Dict[str, Any]:
        """Assembles the end-of-session report from live state.

        Never raises. A reporting failure must not take down the bot.
        """
        try:
            acc = self.broker.get_account_summary()
            equity = float(acc.get("equity", 0.0))
            mode = acc.get("mode", "")
            positions = list(self.broker.get_open_positions().values())

            start = getattr(self.risk_mgr, "start_of_day_equity", None)
            if not start:
                start = equity
            daily_pnl = equity - float(start)
            daily_pnl_pct = (daily_pnl / float(start) * 100.0) if start else 0.0

            warnings = list(self._session_warnings)
            if not self._session_trades and not positions:
                warnings.append(
                    "No trades and no positions. If you expected activity, "
                    "the optimiser may be rejecting every candidate."
                )
            if self._session_scans == 0:
                warnings.append("No market scans completed this session.")

            return {
                "equity": equity,
                "daily_pnl": daily_pnl,
                "daily_pnl_pct": daily_pnl_pct,
                "positions": positions,
                "trades_taken": list(self._session_trades),
                "warnings": warnings,
                "mode": mode,
                "scans": self._session_scans,
            }
        except Exception as e:
            logger.warning(f"Could not build session report: {e}")
            return {}

    def _write_heartbeat(self):
        """Records what the live loop is doing, so another process can tell.

        The running bot holds its session state in memory. A separate
        `bot.py --status-telegram` process cannot see it. The heartbeat
        file bridges that gap: the live loop writes it on every cycle, and
        the status command reads it to report scan count and liveness.

        Writes are atomic so a reader never sees a half-written file.
        """
        try:
            heartbeat = {
                "profile": Config.BOT_PROFILE,
                "pid": os.getpid(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "scans": self._session_scans,
                "cycles": self._cycle_count,
                "last_scan_at": datetime.now(timezone.utc).isoformat(),
                "session_trades": len(self._session_trades),
                "warnings": list(self._session_warnings),
                "market_state": "OPEN" if self._market_open else "CLOSED",
                "started_at": self._started_at,
                "next_open": getattr(self, "_next_open", None),
                "positions_reconciliation": self.broker.positions_reconciliation(),
            }
            tmp = self._heartbeat_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(heartbeat, indent=2), encoding="utf-8")
            tmp.replace(self._heartbeat_path)
        except Exception as e:
            logger.warning(f"Could not write heartbeat: {e}")

    def _read_heartbeat(self) -> Dict[str, Any]:
        """Reads the live loop's heartbeat, if there is a fresh one."""
        try:
            path = getattr(self, "_heartbeat_path", None)
            if not path or not path.exists():
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
            updated = data.get("updated_at", "")
            if updated:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(updated)).total_seconds()
                data["_age_seconds"] = age
            return data
        except Exception:
            return {}

    def _build_bot_status(self) -> Dict[str, Any]:
        """Assembles a live snapshot of what the bot is doing right now.

        Never raises. A status check must not be able to break anything.
        """
        try:
            acc = self.broker.get_account_summary()
            equity = float(acc.get("equity", 0.0))
            last_equity = float(acc.get("last_equity", 0.0) or 0.0)
            positions = list(self.broker.get_open_positions().values())

            # Whether the position view above is trustworthy. During a broker
            # outage get_open_positions() degrades to an empty dict, so we
            # surface the outage here rather than silently showing "flat".
            reconciliation = {"ok": True, "error": None, "source": "unknown"}
            try:
                reconciliation = self.broker.positions_reconciliation()
            except Exception:
                pass

            daily_pnl = equity - last_equity if last_equity else 0.0
            daily_pnl_pct = (daily_pnl / last_equity * 100.0) if last_equity else 0.0

            market_state = "unknown"
            try:
                clock = self.broker.get_market_clock()
                market_state = "OPEN" if clock.get("is_open") else "CLOSED"
            except Exception:
                pass

            # Prefer the live loop's own heartbeat over this process's
            # view, so the report reflects the RUNNING bot rather than a
            # freshly spawned one.
            heartbeat = self._read_heartbeat()
            scans = heartbeat.get("scans", getattr(self, "_session_scans", 0))
            last_scan = ""
            if heartbeat.get("last_scan_at"):
                try:
                    last_scan = datetime.fromisoformat(
                        heartbeat["last_scan_at"]
                    ).astimezone().strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    last_scan = heartbeat["last_scan_at"]
                age = heartbeat.get("_age_seconds")
                if age is not None:
                    if age > 300:
                        last_scan += f" (stale — {int(age)}s ago)"
                    else:
                        last_scan += " (live)"

            warnings = list(heartbeat.get("warnings") or [])

            return {
                "equity": equity,
                "last_equity": last_equity,
                "daily_pnl": daily_pnl,
                "daily_pnl_pct": daily_pnl_pct,
                "positions": positions,
                "mode": acc.get("mode", ""),
                "market_state": market_state,
                "scans": scans,
                "last_scan_at": last_scan,
                "warnings": warnings,
                "heartbeat": heartbeat,
                "positions_reconciliation": reconciliation,
            }
        except Exception as e:
            logger.warning(f"Could not build bot status: {e}")
            return {}

    def _send_bot_status(self):
        """Sends the current-state snapshot. Never allowed to break the loop."""
        notifier = getattr(self, "notifier", None)
        if notifier is None:
            return
        try:
            status = self._build_bot_status()
            if not status:
                return
            notifier.notify_bot_status(status)
            logger.info("Bot status sent.")
        except Exception as e:
            logger.warning(f"Could not send bot status: {e}")

    def _send_session_report(self):
        """Sends the end-of-session report. Never allowed to break the loop."""
        notifier = getattr(self, "notifier", None)
        if notifier is None:
            return
        if not Config.ENABLE_SESSION_REPORT:
            logger.info("Session report disabled by configuration.")
            return
        try:
            report = self._build_session_report()
            if not report:
                return
            notifier.notify_session_report(report)
            logger.info("Session report sent.")
        except Exception as e:
            logger.warning(f"Could not send session report: {e}")

    def scan_and_trade(self) -> list[TradeSignal]:
        """
        Scans all watchlist assets, analyzes regimes, generates signals,
        verifies risk, and executes bracket orders.
        """
        console.print(f"\n[bold magenta]🔍 Scanning Watchlist ({', '.join(Config.WATCHLIST)})...[/bold magenta]")
        signals = []
        price_map = {}

        # --- Protective order audit -------------------------------------
        # Runs BEFORE anything else. A position with no stop-loss is
        # unprotected, and nothing else in the bot would notice: it still
        # shows in the dashboard, still counts toward MAX_OPEN_POSITIONS,
        # and still reports a plausible P&L. This is the one failure that
        # can cost real money with no visible symptom.
        try:
            problems = self.broker.ensure_protective_orders()
            for sym, issues in (problems or {}).items():
                for issue in issues:
                    self._record_session_warning(f"{sym}: {issue}")
                    console.print(f"[bold red]🛡️ {sym}: {issue}[/bold red]")
        except Exception as e:
            logger.warning(f"Protective order audit failed: {e}")

        scan_table = Table(title="📈 Market Scanner & Quantitative Edge Analysis")
        scan_table.add_column("Symbol", style="bold cyan")
        scan_table.add_column("Price", justify="right")
        scan_table.add_column("Regime", style="bold")
        scan_table.add_column("RSI", justify="right")
        scan_table.add_column("Signal", style="bold")
        scan_table.add_column("Confidence", justify="right")
        scan_table.add_column("Strategy Rationale", style="dim")

        acc = self.broker.get_account_summary()

        # --- Fetch all bars up front so the conviction scanner can rank the
        # universe. Without this the bot took every BUY signal it saw, so a
        # weak setup could consume the last position slot ahead of a
        # high-conviction one.
        # Inverse ETFs (SH / PSQ) join the scan universe so the short side can
        # be taken through a plain long bracket when the macro gate is bearish.
        # The macro filter blocks them outside a downtrend.
        universe = list(Config.WATCHLIST)
        if Config.ENABLE_INVERSE_ETFS:
            universe += [s for s in Config.INVERSE_ETFS if s not in universe]

        frames = {}
        for sym in universe:
            df = self.market_data.get_historical_bars(
                sym,
                timeframe=Config.BAR_TIMEFRAME,
                days_back=60
            )
            if not df.empty and len(df) >= 50:
                if Config.DATA_QUALITY_ENABLED and hasattr(self.market_data, "get_quality_report"):
                    report = self.market_data.get_quality_report(sym)
                    if report:
                        skip, reason = report.should_skip()
                        if skip:
                            logger.warning(f"Data quality gate: skipping {sym} ({reason})")
                            continue
                frames[sym] = df

        # Macro market environment evaluation
        macro_state = {}
        if Config.ENABLE_MACRO_TREND_FILTER and hasattr(self, "macro_filter"):
            try:
                macro_state = self.macro_filter.evaluate_macro_environment(self.market_data)
                if macro_state.get("is_bearish"):
                    b_str = ", ".join(macro_state.get("bearish_benchmarks", []))
                    console.print(f"[bold yellow]⚠️ Macro Market Alert: Benchmarks ({b_str}) in Daily TRENDING_BEAR. Longs gated by relative strength.[/bold yellow]")
            except Exception as e:
                logger.warning(f"Macro market evaluation error: {e}")

        # Congressional STOCK Act disclosure watch (advisory, off by default).
        # Logs fresh congressional trades in watchlist tickers; never blocks.
        if getattr(Config, "ENABLE_CONGRESS_WATCH", False):
            try:
                self.risk_mgr.log_congress_advisories(universe)
            except Exception as e:
                logger.warning(f"Congress advisory check failed: {e}")

        # Rank by conviction; the highest-scoring symbols are considered first.
        conviction_order = list(universe)
        conviction_scores = {}
        if frames:
            try:
                ranked = AssetScanner(universe).scan_and_rank(frames)
                ranked_symbols = [r["symbol"] for r in ranked]
                conviction_scores = {r["symbol"]: r["score"] for r in ranked}
                conviction_order = (
                    [s for s in ranked_symbols if s in universe]
                    + [s for s in universe if s not in ranked_symbols]
                )
            except Exception as e:
                logger.warning(f"Conviction ranking failed, falling back to watchlist order: {e}")

        for sym in conviction_order:
            # Re-read the open positions for every symbol. The previous
            # implementation snapshotted them once before the loop, so every
            # symbol saw the same count and MAX_OPEN_POSITIONS could be
            # exceeded within a single scan.
            open_pos = self.broker.get_open_positions()

            df = frames.get(sym)

            if df is None:
                # We may still be holding this symbol. Keep the last known
                # price in the map so its stop loss and take profit are still
                # evaluated -- dropping it would leave the position unmanaged.
                if sym in open_pos:
                    held_price = open_pos[sym].get("current_price")
                    if held_price:
                        price_map[sym] = float(held_price)
                continue

            latest_close = float(df['Close'].iloc[-1])
            price_map[sym] = latest_close

            # Load dynamically self-improved parameters for this specific symbol
            tuned_params = self.self_improver.get_symbol_params(sym)
            strategy = AdaptiveStrategy(params=tuned_params)

            signal = strategy.generate_signal(sym, df)
            signals.append(signal)

            # Color coding for terminal output
            sig_color = "green" if signal.action == "BUY" else ("red" if signal.action == "SELL" else "white")
            reg_color = {
                "TRENDING_BULL": "bold green",
                "TRENDING_BEAR": "bold red",
                "RANGING": "yellow",
                "HIGH_VOLATILITY": "bold magenta"
            }.get(signal.regime, "white")

            rsi_val = signal.metadata.get("rsi", 0.0)

            scan_table.add_row(
                sym,
                f"${latest_close:.2f}",
                f"[{reg_color}]{signal.regime}[/{reg_color}]",
                f"{rsi_val:.1f}",
                f"[{sig_color}]{signal.action}[/{sig_color}]",
                f"{signal.confidence * 100:.0f}%",
                signal.reason
            )

            # If BUY signal, process with institutional risk manager
            if signal.action == "BUY":
                # Check Macro Market Trend Gate
                if Config.ENABLE_MACRO_TREND_FILTER and hasattr(self, "macro_filter"):
                    try:
                        daily_df = self.market_data.get_historical_bars(sym, timeframe="1Day", days_back=60)
                        macro_ok, macro_reason = self.macro_filter.validate_candidate_long(sym, daily_df, macro_state)
                        if not macro_ok:
                            console.print(f"[yellow]⚠️ Signal on {sym} skipped by Macro Trend Gate: {macro_reason}[/yellow]")
                            continue
                    except Exception as e:
                        logger.warning(f"Macro check failed for {sym}: {e}")

                try:
                    size_res = self.risk_mgr.calculate_position_size(
                        signal=signal,
                        portfolio_equity=acc['equity'],
                        cash_available=acc['cash'],
                        current_open_positions_count=len(open_pos),
                        already_holding_symbol=sym in open_pos,
                        open_positions=open_pos
                    )
                except TypeError:
                    size_res = self.risk_mgr.calculate_position_size(
                        signal=signal,
                        portfolio_equity=acc['equity'],
                        cash_available=acc['cash'],
                        current_open_positions_count=len(open_pos),
                        already_holding_symbol=sym in open_pos
                    )

                if size_res.allowed:
                    console.print(
                        f"\n[bold green]🚀 EXECUTING BUY FOR {sym}:[/bold green] "
                        f"Size: {size_res.shares} shares @ ${signal.entry_price:.2f} "
                        f"(Total: ${size_res.cost:,.2f} | Risk: ${size_res.risk_amount:.2f})"
                    )
                    console.print(
                        f"   🛡️ Stop Loss: ${signal.stop_loss_price:.2f} | 🎯 Take Profit: ${signal.take_profit_price:.2f}"
                    )
                    order_type = getattr(Config, "DEFAULT_ORDER_TYPE", "market")
                    if order_type == "limit":
                        order_res = self.broker.place_limit_buy(
                            symbol=sym,
                            qty=size_res.shares,
                            limit_price=signal.entry_price,
                            stop_loss_price=signal.stop_loss_price,
                            take_profit_price=signal.take_profit_price,
                        )
                    elif order_type == "stop_limit":
                        order_res = self.broker.place_stop_limit_buy(
                            symbol=sym,
                            qty=size_res.shares,
                            stop_price=signal.entry_price,
                            limit_price=signal.entry_price * 1.002,
                            stop_loss_price=signal.stop_loss_price,
                            take_profit_price=signal.take_profit_price,
                        )
                    elif order_type == "twap":
                        handle = self.broker.place_twap_buy(
                            symbol=sym,
                            qty=size_res.shares,
                            duration_minutes=5.0,
                            stop_loss_price=signal.stop_loss_price,
                            take_profit_price=signal.take_profit_price,
                        )
                        order_res = OrderResult(
                            success=True,
                            order_id=handle.algo_id,
                            symbol=sym,
                            qty=size_res.shares,
                            entry_price=signal.entry_price,
                            stop_loss=signal.stop_loss_price,
                            take_profit=signal.take_profit_price,
                            message=f"TWAP algo order initiated: {handle.algo_id[:8]}"
                        )
                    elif order_type == "vwap":
                        handle = self.broker.place_vwap_buy(
                            symbol=sym,
                            qty=size_res.shares,
                            duration_minutes=5.0,
                            stop_loss_price=signal.stop_loss_price,
                            take_profit_price=signal.take_profit_price,
                        )
                        order_res = OrderResult(
                            success=True,
                            order_id=handle.algo_id,
                            symbol=sym,
                            qty=size_res.shares,
                            entry_price=signal.entry_price,
                            stop_loss=signal.stop_loss_price,
                            take_profit=signal.take_profit_price,
                            message=f"VWAP algo order initiated: {handle.algo_id[:8]}"
                        )
                    elif order_type == "iceberg":
                        handle = self.broker.place_iceberg_buy(
                            symbol=sym,
                            total_qty=size_res.shares,
                            display_qty=None,
                            stop_loss_price=signal.stop_loss_price,
                            take_profit_price=signal.take_profit_price,
                        )
                        order_res = OrderResult(
                            success=True,
                            order_id=handle.algo_id,
                            symbol=sym,
                            qty=size_res.shares,
                            entry_price=signal.entry_price,
                            stop_loss=signal.stop_loss_price,
                            take_profit=signal.take_profit_price,
                            message=f"Iceberg algo order initiated: {handle.algo_id[:8]}"
                        )
                    else:
                        order_res = self.broker.place_bracket_buy_order(
                            symbol=sym,
                            qty=size_res.shares,
                            entry_price=signal.entry_price,
                            stop_loss_price=signal.stop_loss_price,
                            take_profit_price=signal.take_profit_price
                        )
                    if order_res.success:
                        console.print(f"   ✅ [bold green]{order_res.message}[/bold green]")
                        self._notify_order("BUY", sym, size_res.shares, signal.entry_price, signal.reason)
                        self.journal.record_entry(
                            symbol=sym,
                            qty=size_res.shares,
                            entry_price=signal.entry_price,
                            stop_loss=signal.stop_loss_price,
                            take_profit=signal.take_profit_price,
                            reason=signal.reason,
                        )
                    else:
                        console.print(f"   ❌ [bold red]Order failed: {order_res.message}[/bold red]")
                else:
                    console.print(f"[yellow]⚠️ Signal on {sym} skipped by Risk Manager: {size_res.rejection_reason}[/yellow]")
                    if "circuit breaker" in size_res.rejection_reason.lower():
                        self._notify_circuit_breaker(size_res.rejection_reason, sym)

            # A SELL signal means the regime has turned against the position.
            # Previously these signals were generated with 0.75 confidence and
            # then discarded, so a position could only ever be closed by its
            # own bracket.
            elif signal.action == "SELL":
                position = open_pos.get(sym)
                if not position:
                    continue
                qty = int(position.get("qty", 0))
                if qty <= 0:
                    continue
                console.print(
                    f"\n[bold red]🔻 EXECUTING SELL FOR {sym}:[/bold red] "
                    f"Closing {qty} shares - {signal.reason}"
                )
                close_res = self.broker.place_close_position_order(
                    symbol=sym,
                    qty=qty,
                    reason=signal.reason
                )
                if close_res.success:
                    console.print(f"   ✅ [bold green]{close_res.message}[/bold green]")
                    self._notify_order("SELL", sym, qty, position.get("current_price", 0.0), signal.reason)
                    self.journal.record_full_exit(sym, float(position.get("current_price", 0.0)), signal.reason)
                    if hasattr(self, "_scaled_out_positions"):
                        self._scaled_out_positions.discard(sym)
                else:
                    console.print(f"   ❌ [bold red]Close failed: {close_res.message}[/bold red]")

        console.print(scan_table)

        # Update simulated positions if using local paper broker
        exits = self.broker.update_simulated_prices(price_map)
        for exit_msg in exits:
            console.print(f"[bold yellow]{exit_msg}[/bold yellow]")

        # Record simulated exits in session trades and journal
        sim_exit_details = getattr(self.broker, "last_simulated_exit_details", [])
        if sim_exit_details:
            for ed in sim_exit_details:
                e_sym = ed["symbol"]
                e_qty = ed["qty"]
                e_price = ed["exit_price"]
                e_reason = ed["reason"]
                self._notify_order("SELL", e_sym, e_qty, e_price, e_reason)
                self.journal.record_full_exit(e_sym, e_price, e_reason)
                if hasattr(self, "_scaled_out_positions"):
                    self._scaled_out_positions.discard(e_sym)
            if hasattr(self.broker, "last_simulated_exit_details"):
                self.broker.last_simulated_exit_details = []

        # Apply trailing stops to open positions that have run up.
        self._apply_trailing_stops(price_map)

        return signals

    def _apply_trailing_stops(self, price_map: Dict[str, float]):
        """
        Moves stops up on profitable positions.

        The RiskManager has always calculated a trailing stop, but nothing
        ever called it, so the README's promise to "manage trailing stops"
        was not kept. Breakeven is moved to just above the entry once the
        position is up by a full risk unit, and then trailed by ATR.
        """
        open_pos = self.broker.get_open_positions()
        if not open_pos:
            if hasattr(self, "_scaled_out_positions"):
                self._scaled_out_positions.clear()
            return

        # Keep _scaled_out_positions pruned to currently open symbols only
        if hasattr(self, "_scaled_out_positions"):
            self._scaled_out_positions.intersection_update(set(open_pos.keys()))

        for sym, position in open_pos.items():
            entry = float(position.get("entry_price", 0.0))
            current = float(position.get("current_price", 0.0))
            if current > 0:
                self.journal.update_price_excursions(sym, current)
            current_stop = float(position.get("stop_loss", 0.0))
            if entry <= 0 or current <= 0 or current_stop <= 0:
                continue

            df = self.market_data.get_historical_bars(
                sym, timeframe=Config.BAR_TIMEFRAME, days_back=60
            )
            atr = 0.0
            if not df.empty and len(df) >= 20:
                try:
                    from indicators.technicals import TechnicalIndicators
                    ind = TechnicalIndicators.add_all_indicators(df)
                    atr_value = ind['ATR'].iloc[-1]
                    atr = 0.0 if pd.isna(atr_value) else float(atr_value)
                except Exception:
                    atr = 0.0
            if atr <= 0:
                atr = current * 0.015

            qty = int(position.get("qty", 0))

            # --- Partial Profit-Taking (Scale-Out) ---
            initial_risk = max(entry - current_stop, atr * 1.5)
            unrealized_gain = current - entry
            scaled_out_set = getattr(self, "_scaled_out_positions", set())

            if (
                Config.ENABLE_PARTIAL_PROFIT_TAKING
                and sym not in scaled_out_set
                and qty >= 2
                and unrealized_gain >= (Config.SCALE_OUT_RR * initial_risk)
            ):
                scale_qty = int(qty * Config.SCALE_OUT_PERCENT)
                if scale_qty > 0:
                    rr_achieved = unrealized_gain / initial_risk if initial_risk > 0 else 1.0
                    console.print(
                        f"\n[bold green]🎯 PARTIAL PROFIT SCALE-OUT FOR {sym}:[/bold green] "
                        f"Booking {scale_qty} of {qty} shares @ ${current:.2f} "
                        f"(Gain: +{unrealized_gain / entry * 100:.2f}% | {rr_achieved:.1f}R)"
                    )
                    close_res = self.broker.place_close_position_order(
                        symbol=sym,
                        qty=scale_qty,
                        reason=f"Scale-out: 50% profit booked at {rr_achieved:.1f}R"
                    )
                    if close_res.success:
                        if not hasattr(self, "_scaled_out_positions"):
                            self._scaled_out_positions = set()
                        self._scaled_out_positions.add(sym)
                        console.print(f"   ✅ [bold green]{close_res.message}[/bold green]")
                        self._notify_order(
                            "PARTIAL_CLOSE", sym, scale_qty, current,
                            f"Scale-out booked at {rr_achieved:.1f}R"
                        )
                        self.journal.record_partial_exit(
                            symbol=sym,
                            qty=scale_qty,
                            exit_price=current,
                            reason=f"Scale-out: 50% profit booked at {rr_achieved:.1f}R",
                        )
                        # Move stop on remaining shares to breakeven + fee buffer immediately
                        breakeven_stop = round(entry + (atr * 0.2), 2)
                        if breakeven_stop > current_stop:
                            if self.broker.update_trailing_stop(sym, breakeven_stop):
                                console.print(
                                    f"   🛡️ Stop for remaining {qty - scale_qty} shares moved to Breakeven: ${breakeven_stop:.2f}"
                                )
                        continue

            new_stop = self.risk_mgr.calculate_trailing_stop(
                entry_price=entry,
                current_price=current,
                current_stop=current_stop,
                atr=atr,
                atr_multiplier=1.8,
            )

            if new_stop > current_stop:
                if self.broker.update_trailing_stop(sym, new_stop):
                    console.print(
                        f"   📈 Trailing stop for {sym} raised: "
                        f"${current_stop:.2f} → ${new_stop:.2f}"
                    )

    def run_self_improvement_cycle(self, target_symbol: str = None):
        """Runs the walk-forward optimizer to tune strategy parameters."""
        symbols = [target_symbol.upper()] if target_symbol else Config.WATCHLIST
        console.print(f"\n[bold cyan]🧠 RUNNING SELF-IMPROVEMENT FOR: {', '.join(symbols)}...[/bold cyan]")
        opt_table = Table(title="🔬 Parameter Evolution & Walk-Forward Validation")
        opt_table.add_column("Symbol", style="bold cyan")
        opt_table.add_column("Status", style="bold")
        opt_table.add_column("Baseline Fitness", justify="right")
        opt_table.add_column("Optimized Win%", justify="right")
        opt_table.add_column("Sharpe Ratio", justify="right")
        opt_table.add_column("Fitness Score", justify="right")
        opt_table.add_column("Tuned Parameters", style="dim")

        # Benchmark for alpha scoring. Fetched once for the whole run.
        benchmark_df = SelfImprover.fetch_benchmark(self.market_data)

        for sym in symbols:
            df = self.market_data.get_historical_bars(
                sym,
                timeframe=Config.OPTIMIZATION_TIMEFRAME,
                days_back=Config.OPTIMIZATION_LOOKBACK_DAYS
            )
            if df.empty:
                opt_table.add_row(sym, "[yellow]Skipped[/yellow]", "-", "-", "-", "-", "No data")
                continue

            res = self.self_improver.optimize_symbol(sym, df, benchmark_df=benchmark_df)
            if res.get("status") == "skipped":
                opt_table.add_row(sym, "[yellow]Skipped[/yellow]", "-", "-", "-", "-", res.get("reason"))
                continue

            improved = res.get("improved", False)
            status_str = "[bold green]IMPROVED ✨[/bold green]" if improved else "[blue]Validated[/blue]"
            perf = res.get("performance", {})
            params = res.get("best_params", {})
            param_summary = f"EMA: {params.get('ema_fast')}/{params.get('ema_slow')} | RSI: {params.get('rsi_buy_threshold')} | ATR: {params.get('atr_stop_multiplier')}x"

            opt_table.add_row(
                sym,
                status_str,
                f"{res.get('baseline_fitness', 0):.2f}",
                f"{perf.get('win_rate', 0):.1f}%",
                f"{perf.get('sharpe_ratio', 0):.2f}",
                f"{perf.get('fitness_score', 0):.2f}",
                param_summary
            )

            # If the optimiser changed the strategy under the operator's
            # feet, that belongs in the session report. Adopting new
            # parameters changes what the bot will do tomorrow, so it is
            # not something to bury in a log file.
            if improved:
                self._record_session_warning(
                    f"Optimiser adopted new parameters for {sym} "
                    f"(fitness {res.get('fitness_margin', 0):+.2f} vs baseline)."
                )

        console.print(opt_table)
        console.print("[green]✅ Strategy parameters successfully updated and saved to strategy_state.json[/green]\n")

    def run_backtest_symbol(self, symbol: str):
        """Runs an in-depth backtest report for a chosen symbol."""
        console.print(f"\n[bold cyan]📊 Running Backtest Report for {symbol}...[/bold cyan]")
        df = self.market_data.get_historical_bars(symbol, timeframe=Config.BAR_TIMEFRAME, days_back=90)
        if df.empty:
            console.print(f"[red]Could not fetch historical data for {symbol}[/red]")
            return

        params = self.self_improver.get_symbol_params(symbol)
        benchmark_df = SelfImprover.fetch_benchmark(self.market_data)
        # The backtest trades from bar 50 (indicator warm-up) to the last bar.
        benchmark_return_pct = SelfImprover._benchmark_return_pct(
            benchmark_df, df.index[50], df.index[-1]
        )
        res = StrategyBacktester.run_backtest(
            df, params, symbol=symbol, benchmark_return_pct=benchmark_return_pct
        )

        bt_table = Table(title=f"📈 Quantitative Performance Report: {symbol}")
        bt_table.add_column("Metric", style="bold")
        bt_table.add_column("Value", style="cyan")

        bt_table.add_row("Total Trades", str(res.total_trades))
        bt_table.add_row("Winning Trades", str(res.winning_trades))
        bt_table.add_row("Losing Trades", str(res.losing_trades))
        bt_table.add_row("Win Rate", f"{res.win_rate:.1f}%")
        bt_table.add_row("Profit Factor", f"{res.profit_factor:.2f}")
        bt_table.add_row("Total Return", f"{res.total_pnl_pct:+.2f}%")
        bt_table.add_row("Alpha vs Benchmark", f"{res.alpha_pct:+.2f}%")
        bt_table.add_row("Max Drawdown", f"{res.max_drawdown_pct:.2f}%")
        bt_table.add_row("Annualized Sharpe Ratio", f"{res.sharpe_ratio:.2f}")
        bt_table.add_row("Overall Fitness Score", f"{res.fitness_score:.2f}")

        console.print(bt_table)

    def start_live_loop(self, interval_seconds: int = 60, auto_calibrate: bool = True):
        """
        All-in-one Autonomous Trading Engine with Market-Hours Awareness:
        1. Automatically sleeps when the market is closed and wakes before the next open.
        2. Calibrates parameters via self-improvement before each trading day (if stale).
        3. Enters continuous scan, order placement, and risk management loop during market hours.
        4. Resets the daily circuit breaker at the start of each new trading day.
        5. Automatically triggers walk-forward optimization on schedule.
        """
        console.print(Panel(
            "[bold green]AUTONOMOUS AI TRADING MODE ACTIVATED[/bold green]\n"
            f"• Watchlist: [cyan]{', '.join(Config.WATCHLIST)}[/cyan]\n"
            f"• Scan Interval: [yellow]{interval_seconds} seconds[/yellow]\n"
            f"• Market Hours: [cyan]Mon–Fri 9:30 AM – 4:00 PM ET (auto-sleep when closed)[/cyan]\n"
            f"• Self-Improvement: [magenta]{'Enabled (Every 24h)' if Config.ENABLE_SELF_IMPROVEMENT else 'Disabled'}[/magenta]\n"
            f"• Risk Per Trade: [bold red]{Config.MAX_PORTFOLIO_RISK_PER_TRADE * 100:.1f}% max portfolio risk[/bold red]\n"
            "• Press [bold yellow]Ctrl+C[/bold yellow] at any time to pause safely.",
            title="⚡ [bold]AI Trading Bot Auto-Pilot[/bold] ⚡",
            expand=False
        ))

        cycle_count = 0
        last_opt_time = time.time()
        opt_interval_seconds = Config.OPTIMIZATION_INTERVAL_HOURS * 3600
        current_trading_date = None  # Track the current trading day for reset logic

        try:
            while True:
                # ── Market Hours Check ──────────────────────────────
                clock = self.broker.get_market_clock()
                is_open = clock.get("is_open", False)

                # ── Session Close Detection ─────────────────────────
                # The report fires on the open -> closed TRANSITION. This
                # matters: the bot may be started after the market is
                # already shut, or restarted mid-session, and neither
                # should produce a report for a session the bot did not
                # actually observe.
                if is_open:
                    self._was_market_open = True
                    self._market_open = True
                elif self._was_market_open:
                    # The market just closed. Send the report for the
                    # session that just ended, then reset the session
                    # state so tomorrow starts clean.
                    self._was_market_open = False
                    self._market_open = False
                    self._write_heartbeat()
                    today_et = datetime.now(timezone.utc).date()
                    if self._report_sent_for_date != today_et:
                        self._report_sent_for_date = today_et
                        console.print("\n[bold cyan]📬 Market closed. Sending session report...[/bold cyan]")
                        self._send_session_report()
                        self._session_trades.clear()
                        self._session_warnings.clear()
                        self._session_scans = 0

                if not is_open:
                    next_open = clock.get("next_open")
                    if next_open:
                        try:
                            if hasattr(next_open, 'timestamp'):
                                wait_seconds = max(0, (next_open - datetime.now(timezone.utc)).total_seconds())
                            else:
                                wait_seconds = 60  # Fallback: check again in 60s
                        except Exception:
                            wait_seconds = 60

                        # Cap the sleep display and wake up 2 minutes early for pre-market calibration
                        wake_early = max(0, wait_seconds - 120)
                        hours_left = wake_early / 3600

                        # Report liveness even while closed. Without this the
                        # dashboard sees "NO HEARTBEAT / market UNKNOWN" all
                        # evening, indistinguishable from a dead bot.
                        self._next_open = (
                            next_open.isoformat() if hasattr(next_open, "isoformat")
                            else (str(next_open) if next_open else None)
                        )
                        self._write_heartbeat()

                        if hours_left > 0.1:
                            console.print(Panel(
                                f"[bold yellow]🌙 Market is CLOSED[/bold yellow]\n"
                                f"• Next Market Open: [cyan]{next_open}[/cyan]\n"
                                f"• Sleeping for [bold]{hours_left:.1f} hours[/bold]...\n"
                                f"• The bot will auto-wake 2 minutes before open for pre-market calibration.",
                                title="💤 [bold]Market Closed – Auto-Sleep[/bold]",
                                expand=False
                            ))
                            logger.info(f"Market closed. Sleeping until ~{next_open} ({hours_left:.1f}h)")

                            # Sleep in 5-minute chunks so Ctrl+C remains responsive
                            sleep_remaining = wake_early
                            while sleep_remaining > 0:
                                chunk = min(sleep_remaining, 300)  # 5 min chunks
                                time.sleep(chunk)
                                sleep_remaining -= chunk

                            console.print("\n[bold cyan]☀️ Waking up for pre-market preparation...[/bold cyan]")
                        else:
                            # Very close to open, short wait
                            console.print(f"[dim]Market opens shortly. Checking again in 30s...[/dim]")
                            time.sleep(30)
                    else:
                        # Weekend or no next_open info — check again in 5 minutes
                        console.print("[dim]Market closed (weekend/holiday). Checking again in 5 minutes...[/dim]")
                        time.sleep(300)
                    continue

                # ── New Trading Day Reset ───────────────────────────
                today = datetime.now(timezone.utc).date()
                if current_trading_date != today:
                    current_trading_date = today
                    console.print(f"\n[bold green]📅 New Trading Day: {today.isoformat()}[/bold green]")

                    # Reset daily circuit breaker
                    acc = self.broker.get_account_summary()
                    self.risk_mgr.start_of_day_equity = None
                    self.risk_mgr.circuit_breaker_tripped = False
                    self.risk_mgr.update_start_of_day_equity(acc['equity'])
                    logger.info(f"Daily reset complete. Start-of-day equity: ${acc['equity']:,.2f}")

                    # Prune or reset scale-out state for closed positions
                    if hasattr(self, "_scaled_out_positions"):
                        active_syms = set(self.broker.get_open_positions().keys())
                        self._scaled_out_positions.intersection_update(active_syms)

                    # Pre-market calibration if parameters are stale
                    if auto_calibrate and Config.ENABLE_SELF_IMPROVEMENT:
                        last_opt = self.self_improver.state.get("last_updated")
                        needs_calibration = True
                        if last_opt:
                            try:
                                last_dt = datetime.fromisoformat(last_opt)
                                age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600.0
                                if age_hours < Config.OPTIMIZATION_INTERVAL_HOURS:
                                    needs_calibration = False
                                    console.print(f"[dim]Parameters are up-to-date (calibrated {age_hours:.1f}h ago). Skipping pre-market tuning.[/dim]")
                            except Exception:
                                needs_calibration = True

                        if needs_calibration:
                            console.print("\n[bold cyan]🔄 Pre-Market Calibration: Tuning strategy parameters...[/bold cyan]")
                            self.run_self_improvement_cycle()
                            last_opt_time = time.time()

                # ── Active Trading Cycle ────────────────────────────
                cycle_count += 1
                self._session_scans += 1
                self._cycle_count = cycle_count
                self._market_open = True
                console.print(f"\n[bold blue]─── Autopilot Cycle #{cycle_count} ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ───[/bold blue]")
                self.print_dashboard_status()
                self.scan_and_trade()
                self._write_heartbeat()

                # Periodic background self-improvement during the session
                if Config.ENABLE_SELF_IMPROVEMENT and (time.time() - last_opt_time) >= opt_interval_seconds:
                    console.print("\n[bold magenta]⏰ Scheduled Self-Improvement Cycle Triggered...[/bold magenta]")
                    self.run_self_improvement_cycle()
                    last_opt_time = time.time()

                console.print(f"\n[dim]Next market scan in {interval_seconds}s... (Press Ctrl+C to stop)[/dim]")
                time.sleep(interval_seconds)

        except KeyboardInterrupt:
            console.print("\n[bold yellow]Trading bot safely stopped by user.[/bold yellow]")
            self.print_dashboard_status()


def _print_paper_forward_summary(summary: dict) -> None:
    """Human-readable output for the paper-forward validation command."""
    if not summary:
        return
    print(f"Paper-forward snapshots: {summary.get('snapshots', 0)}")
    print(f"Observed weeks: {summary.get('observed_weeks', 0.0)}")
    result = summary.get("result")
    if result is not None:
        print(
            f"Strategy return: {result.strategy_return_pct:.2f}%  "
            f"Baseline: {result.baseline_return_pct:.2f}%  "
            f"Alpha: {result.alpha_pct:.2f}%"
        )
        print(
            f"Strategy Sharpe: {result.strategy_sharpe:.2f}  "
            f"Baseline Sharpe: {result.baseline_sharpe:.2f}"
        )
        print(f"Strategy max drawdown: {result.strategy_max_drawdown_pct:.2f}%")
        print(f"Beats baseline (risk-adjusted): {result.beats_baseline}")
    print(f"Stop status: {summary.get('status', 'unknown')}")
    for reason in summary.get("reasons", []):
        print(f"  - {reason}")


def _default_journal_path() -> str:
    # The journal is the owner's validation data, not code -- persist it under
    # validation/ (gitignored) so a run survives restarts.
    return os.path.join(os.path.dirname(__file__), "validation", "paper_forward_journal.json")


def _run_paper_forward_record(bot: "TradingBotOrchestrator", args) -> None:
    from validation.runbook_cli import record_paper_forward_snapshot
    journal_path = args.journal_path or _default_journal_path()
    try:
        summary = record_paper_forward_snapshot(
            bot.broker,
            baseline_symbol=args.baseline_symbol,
            baseline_price=args.baseline_price,
            journal_path=journal_path,
        )
    except Exception as e:
        print(f"Paper-forward record failed: {e}")
        return
    _print_paper_forward_summary(summary)


def _run_paper_forward_evaluate(args) -> None:
    from validation.runbook_cli import evaluate_run
    journal_path = args.journal_path or _default_journal_path()
    try:
        summary = evaluate_run(journal_path=journal_path)
    except Exception as e:
        print(f"Paper-forward evaluate failed: {e}")
        return
    _print_paper_forward_summary(summary)


def main():
    parser = argparse.ArgumentParser(description="Automated AI Trading Bot with Self-Improvement")
    parser.add_argument("--scan", action="store_true", help="Perform a single market scan only")
    parser.add_argument("--optimize", action="store_true", help="Run the self-improvement parameter optimization cycle once")
    parser.add_argument("--symbol", type=str, help="Target specific symbol for optimization (optional)")
    parser.add_argument("--backtest", type=str, help="Run backtest analysis on a specific symbol (e.g., SPY, AAPL)")
    parser.add_argument("--status", action="store_true", help="Show current account equity, open positions, and state only")
    parser.add_argument("--status-telegram", action="store_true",
                        help="Send a live status snapshot (equity, daily P&L, positions, liveness) to Telegram/Discord and exit")
    parser.add_argument("--report", action="store_true", help="Send the session report to Telegram/Discord now and exit")
    parser.add_argument("--run", action="store_true", help="Start autonomous trading (market-hours aware)")
    parser.add_argument("--interval", type=int, default=60, help="Scan interval in seconds (default: 60)")
    parser.add_argument("--paper-forward", action="store_true",
                        help="Record a daily snapshot for the prospective paper-forward validation run")
    parser.add_argument("--paper-forward-evaluate", action="store_true",
                        help="Evaluate the paper-forward stop criteria against the existing journal")
    parser.add_argument("--baseline-symbol", type=str, default="SPY",
                        help="Baseline symbol to beat (default: SPY)")
    parser.add_argument("--baseline-price", type=float, default=None,
                        help="Current baseline price (overrides the data fetch; useful offline)")
    parser.add_argument("--journal-path", type=str, default=None,
                        help="Path to the paper-forward journal (default: validation/paper_forward_journal.json)")

    args = parser.parse_args()
    bot = TradingBotOrchestrator()

    if args.paper_forward:
        _run_paper_forward_record(bot, args)
    elif args.paper_forward_evaluate:
        _run_paper_forward_evaluate(args)
    elif args.optimize:
        bot.run_self_improvement_cycle(target_symbol=args.symbol)
    elif args.backtest:
        bot.run_backtest_symbol(args.backtest.upper())
    elif args.status:
        bot.print_dashboard_status()
    elif args.status_telegram:
        bot._send_bot_status()
    elif args.report:
        bot._send_session_report()
    elif args.scan:
        bot.print_dashboard_status()
        bot.scan_and_trade()
    else:
        # Default behavior: Launch full continuous autonomous mode!
        bot.start_live_loop(interval_seconds=args.interval)

if __name__ == "__main__":
    main()
