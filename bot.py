import sys
import time
import argparse
import logging
from datetime import datetime, timezone
from typing import Dict, Any

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from config import Config
from data.market_data import MarketDataProvider
from strategy.adaptive_strategy import AdaptiveStrategy, TradeSignal
from risk.risk_manager import RiskManager
from optimizer.self_improver import SelfImprover
from optimizer.backtester import StrategyBacktester
from execution.alpaca_broker import AlpacaBroker

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

    def __init__(self):
        self.market_data = MarketDataProvider()
        self.broker = AlpacaBroker()
        self.risk_mgr = RiskManager()
        self.self_improver = SelfImprover()

        # Initialize start of day equity
        acc = self.broker.get_account_summary()
        self.risk_mgr.update_start_of_day_equity(acc['equity'])

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

    def scan_and_trade(self) -> list[TradeSignal]:
        """
        Scans all watchlist assets, analyzes regimes, generates signals,
        verifies risk, and executes bracket orders.
        """
        console.print(f"\n[bold magenta]🔍 Scanning Watchlist ({', '.join(Config.WATCHLIST)})...[/bold magenta]")
        signals = []
        price_map = {}

        scan_table = Table(title="📈 Market Scanner & Quantitative Edge Analysis")
        scan_table.add_column("Symbol", style="bold cyan")
        scan_table.add_column("Price", justify="right")
        scan_table.add_column("Regime", style="bold")
        scan_table.add_column("RSI", justify="right")
        scan_table.add_column("Signal", style="bold")
        scan_table.add_column("Confidence", justify="right")
        scan_table.add_column("Strategy Rationale", style="dim")

        acc = self.broker.get_account_summary()
        open_pos = self.broker.get_open_positions()

        for sym in Config.WATCHLIST:
            df = self.market_data.get_historical_bars(
                sym,
                timeframe=Config.BAR_TIMEFRAME,
                days_back=60
            )
            if df.empty or len(df) < 50:
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
                    order_res = self.broker.place_bracket_buy_order(
                        symbol=sym,
                        qty=size_res.shares,
                        entry_price=signal.entry_price,
                        stop_loss_price=signal.stop_loss_price,
                        take_profit_price=signal.take_profit_price
                    )
                    if order_res.success:
                        console.print(f"   ✅ [bold green]{order_res.message}[/bold green]")
                    else:
                        console.print(f"   ❌ [bold red]Order failed: {order_res.message}[/bold red]")
                else:
                    console.print(f"[yellow]⚠️ Signal on {sym} skipped by Risk Manager: {size_res.rejection_reason}[/yellow]")

        console.print(scan_table)

        # Update simulated positions if using local paper broker
        exits = self.broker.update_simulated_prices(price_map)
        for exit_msg in exits:
            console.print(f"[bold yellow]{exit_msg}[/bold yellow]")

        return signals

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

        for sym in symbols:
            df = self.market_data.get_historical_bars(
                sym,
                timeframe=Config.BAR_TIMEFRAME,
                days_back=Config.OPTIMIZATION_LOOKBACK_DAYS
            )
            if df.empty:
                opt_table.add_row(sym, "[yellow]Skipped[/yellow]", "-", "-", "-", "-", "No data")
                continue

            res = self.self_improver.optimize_symbol(sym, df)
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
        res = StrategyBacktester.run_backtest(df, params, symbol=symbol)

        bt_table = Table(title=f"📈 Quantitative Performance Report: {symbol}")
        bt_table.add_column("Metric", style="bold")
        bt_table.add_column("Value", style="cyan")

        bt_table.add_row("Total Trades", str(res.total_trades))
        bt_table.add_row("Winning Trades", str(res.winning_trades))
        bt_table.add_row("Losing Trades", str(res.losing_trades))
        bt_table.add_row("Win Rate", f"{res.win_rate:.1f}%")
        bt_table.add_row("Profit Factor", f"{res.profit_factor:.2f}")
        bt_table.add_row("Total Return", f"{res.total_pnl_pct:+.2f}%")
        bt_table.add_row("Max Drawdown", f"{res.max_drawdown_pct:.2f}%")
        bt_table.add_row("Annualized Sharpe Ratio", f"{res.sharpe_ratio:.2f}")
        bt_table.add_row("Overall Fitness Score", f"{res.fitness_score:.2f}")

        console.print(bt_table)

    def start_live_loop(self, interval_seconds: int = 60, auto_calibrate: bool = True):
        """
        All-in-one Autonomous Trading Engine:
        1. Calibrates parameters via self-improvement on startup (if needed).
        2. Enters continuous scan, order placement, and risk management loop.
        3. Automatically triggers walk-forward optimization on schedule.
        """
        console.print(Panel(
            "[bold green]AUTONOMOUS AI TRADING MODE ACTIVATED[/bold green]\n"
            f"• Watchlist: [cyan]{', '.join(Config.WATCHLIST)}[/cyan]\n"
            f"• Scan Interval: [yellow]{interval_seconds} seconds[/yellow]\n"
            f"• Self-Improvement: [magenta]{'Enabled (Every 24h)' if Config.ENABLE_SELF_IMPROVEMENT else 'Disabled'}[/magenta]\n"
            f"• Risk Per Trade: [bold red]{Config.MAX_PORTFOLIO_RISK_PER_TRADE * 100:.1f}% max portfolio risk[/bold red]\n"
            "• Press [bold yellow]Ctrl+C[/bold yellow] at any time to pause safely.",
            title="⚡ [bold]AI Trading Bot Auto-Pilot[/bold] ⚡",
            expand=False
        ))

        # Initial Calibration Check: If not calibrated today, tune parameters first
        if auto_calibrate and Config.ENABLE_SELF_IMPROVEMENT:
            last_opt = self.self_improver.state.get("last_updated")
            needs_calibration = True
            if last_opt:
                try:
                    last_dt = datetime.fromisoformat(last_opt)
                    age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600.0
                    if age_hours < Config.OPTIMIZATION_INTERVAL_HOURS:
                        needs_calibration = False
                        console.print(f"[dim]Parameters are up-to-date (calibrated {age_hours:.1f} hours ago). Skipping initial re-tuning.[/dim]")
                except Exception:
                    needs_calibration = True

            if needs_calibration:
                console.print("\n[bold cyan]🔄 Calibrating strategy parameters to current market conditions...[/bold cyan]")
                self.run_self_improvement_cycle()

        cycle_count = 0
        last_opt_time = time.time()
        opt_interval_seconds = Config.OPTIMIZATION_INTERVAL_HOURS * 3600

        try:
            while True:
                cycle_count += 1
                console.print(f"\n[bold blue]─── Autopilot Cycle #{cycle_count} ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ───[/bold blue]")
                self.print_dashboard_status()
                self.scan_and_trade()

                # Periodic background self-improvement
                if Config.ENABLE_SELF_IMPROVEMENT and (time.time() - last_opt_time) >= opt_interval_seconds:
                    console.print("\n[bold magenta]⏰ Scheduled Self-Improvement Cycle Triggered...[/bold magenta]")
                    self.run_self_improvement_cycle()
                    last_opt_time = time.time()

                console.print(f"\n[dim]Next market scan in {interval_seconds}s... (Press Ctrl+C to stop)[/dim]")
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            console.print("\n[bold yellow]Trading bot safely stopped by user.[/bold yellow]")
            self.print_dashboard_status()


def main():
    parser = argparse.ArgumentParser(description="Automated AI Trading Bot with Self-Improvement")
    parser.add_argument("--scan", action="store_true", help="Perform a single market scan only")
    parser.add_argument("--optimize", action="store_true", help="Run the self-improvement parameter optimization cycle once")
    parser.add_argument("--symbol", type=str, help="Target specific symbol for optimization (optional)")
    parser.add_argument("--backtest", type=str, help="Run backtest analysis on a specific symbol (e.g., SPY, AAPL)")
    parser.add_argument("--status", action="store_true", help="Show current account equity, open positions, and state only")
    parser.add_argument("--interval", type=int, default=60, help="Scan interval in seconds (default: 60)")

    args = parser.parse_args()
    bot = TradingBotOrchestrator()

    if args.optimize:
        bot.run_self_improvement_cycle(target_symbol=args.symbol)
    elif args.backtest:
        bot.run_backtest_symbol(args.backtest.upper())
    elif args.status:
        bot.print_dashboard_status()
    elif args.scan:
        bot.print_dashboard_status()
        bot.scan_and_trade()
    else:
        # Default behavior: Launch full continuous autonomous mode!
        bot.start_live_loop(interval_seconds=args.interval)

if __name__ == "__main__":
    main()
