from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any, Callable, TypeVar
import numpy as np
import pandas as pd
from config import Config
from strategy.adaptive_strategy import AdaptiveStrategy, StrategyParameters, MIN_ENTRY_CONFIDENCE

T = TypeVar("T")

# A Sharpe ratio above this is not a measure of edge -- it is an artefact of
# a tiny sample. Real money managers do not sustain Sharpe ratios above ~3.
MAX_PLAUSIBLE_SHARPE = 5.0

# Rows of indicator history handed to the strategy for each bar. The strategy
# only reads the last two rows and the mean-reversion sleeve needs at least 25,
# so a short trailing window gives the same signal as the whole history.
SIGNAL_WINDOW_BARS = 30


@dataclass
class BacktestResult:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl_pct: float
    profit_factor: float
    max_drawdown_pct: float
    sharpe_ratio: float
    fitness_score: float
    # Return in excess of the benchmark over the same window. A strategy that
    # merely rides the market has alpha ~ 0; only genuine edge is rewarded.
    # Defaults to the raw return when no benchmark is supplied (so a direct
    # backtest without a benchmark behaves exactly as before).
    alpha_pct: float = 0.0
    # Slippage plus commission paid, summed over all trades, as a percentage of
    # trade notional. It is already deducted from every figure above; this is
    # reported so the size of the cost drag is visible.
    cost_drag_pct: float = 0.0


@dataclass(frozen=True)
class CostModel:
    """Trading costs charged on every simulated fill.

    * Slippage is adverse: buys fill above, and market-type sells below, the
      reference price. A take-profit is a resting limit order, so it fills at
      its limit with no slippage.
    * Commission is charged per side, as basis points of notional plus an
      optional per-share amount.
    """
    slippage_bps: float = 0.0
    commission_bps: float = 0.0
    commission_per_share: float = 0.0
    exit_slippage: bool = True

    @classmethod
    def from_config(cls) -> "CostModel":
        return cls(
            slippage_bps=Config.BACKTEST_SLIPPAGE_BPS,
            commission_bps=Config.BACKTEST_COMMISSION_BPS,
            commission_per_share=Config.BACKTEST_COMMISSION_PER_SHARE,
            exit_slippage=Config.BACKTEST_EXIT_SLIPPAGE,
        )

    @classmethod
    def zero(cls) -> "CostModel":
        """No costs at all: the old, frictionless behaviour (for comparison)."""
        return cls(0.0, 0.0, 0.0, False)

    def buy_fill(self, price: float) -> float:
        return price * (1.0 + self.slippage_bps / 10_000.0)

    def sell_fill(self, price: float, market: bool) -> float:
        """`market` is True for stop and signal exits, False for a resting limit."""
        if market and self.exit_slippage:
            return max(price * (1.0 - self.slippage_bps / 10_000.0), 0.01)
        return price

    def commission_pct(self, fill_price: float) -> float:
        """Commission for one side, as a fraction of the notional."""
        pct = self.commission_bps / 10_000.0
        if self.commission_per_share and fill_price > 0:
            pct += self.commission_per_share / fill_price
        return pct


class StrategyBacktester:
    """Fast vectorized & event backtester for parameter evaluation."""

    @staticmethod
    def _annualised_sharpe(equity_curve: List[float], index) -> float:
        """Annualised Sharpe ratio computed from DAILY equity returns.

        Two mistakes are avoided here:

        1. Annualising a handful of TRADE returns with sqrt(252) implicitly
           claims the strategy trades 252 times a year. With 7 trades over
           200 bars that is a claim of 36 years of trading, and the ratio
           exploded into the hundreds (measured: Sharpe 1096).
        2. Annualising per-bar returns of a 15-minute series by sqrt(6500)
           produces equally nonsensical numbers.

        Resampling the marked-to-market equity curve to daily closes and
        annualising with sqrt(252) is the standard approach.
        """
        if len(equity_curve) < 3:
            return 0.0

        if isinstance(index, pd.DatetimeIndex):
            series = pd.Series(equity_curve, index=index)
            daily = series.resample("1D").last().dropna()
            daily_returns = daily.pct_change().dropna()
        else:
            # No timestamps available; treat each bar as one period.
            values = np.array(equity_curve, dtype=float)
            daily_returns = pd.Series(values).pct_change().dropna()

        if len(daily_returns) < 2:
            return 0.0

        std = float(np.std(daily_returns))
        if std <= 0:
            return 0.0

        sharpe = (float(np.mean(daily_returns)) / std) * np.sqrt(252.0)

        # A Sharpe ratio beyond a handful is an artefact of a tiny sample or
        # of near-perfect synthetic data, not a measure of edge. Clipping
        # keeps it from dominating the fitness score.
        return float(np.clip(sharpe, -MAX_PLAUSIBLE_SHARPE, MAX_PLAUSIBLE_SHARPE))

    @staticmethod
    def run_backtest(
        df: pd.DataFrame,
        params: StrategyParameters,
        symbol: str = "TICKER",
        initial_capital: float = 10000.0,
        start_idx: int = 50,
        end_idx: Optional[int] = None,
        benchmark_return_pct: Optional[float] = None,
        costs: Optional[CostModel] = None
    ) -> BacktestResult:
        """Run the strategy over a window of an indicator-enriched frame.

        Entries use the live bot's own decision code: for every bar the
        backtester calls `AdaptiveStrategy.signal_from_indicators`, the same
        method `generate_signal` uses live, and enters on a BUY whose
        confidence clears the risk manager's `MIN_ENTRY_CONFIDENCE`. The stop
        and target come from the signal itself. Regime handling (including
        the mean-reversion sleeve in ranging markets) therefore cannot drift
        from what the bot trades.

        Not modelled, because they depend on live data a backtest does not
        have: the spread, news, earnings, macro, correlation and mandate
        gates, position-count limits, and position sizing. A backtest shows
        the signal's trades, not necessarily every trade the bot would take
        or skip.

        Costs: every fill is charged slippage and commission (see CostModel;
        defaults come from Config.BACKTEST_*). A stop that is gapped through
        fills at the bar's open, not at the stop price. Pass
        `costs=CostModel.zero()` for a frictionless run.

        Indicators are computed ONCE on the full frame, then the backtest
        trades only the [start_idx, end_idx) window. This is the point-in-time
        fix: a walk-forward test slice must be evaluated with indicators that
        were warmed up on the real history, not recomputed from the slice's
        own leading bars (which is a different strategy than the live bot
        runs). Because every indicator here is causal, the values in the
        train window are unchanged; only the test window gains the correct
        warm-up.

        `benchmark_return_pct` is the benchmark's percent return over the same
        window. When given, the fitness score rewards excess return over the
        benchmark (alpha) rather than raw return -- so the optimizer cannot
        mistake riding a bull market for edge. When omitted, the raw return is
        used, keeping direct backtests unchanged.
        """
        if df.empty or len(df) < 50:
            return BacktestResult(
                total_trades=0, winning_trades=0, losing_trades=0,
                win_rate=0.0, total_pnl_pct=0.0, profit_factor=0.0,
                max_drawdown_pct=0.0, sharpe_ratio=0.0, fitness_score=-999.0
            )

        costs = costs if costs is not None else CostModel.from_config()
        strategy = AdaptiveStrategy(params=params)
        from indicators.technicals import TechnicalIndicators
        df_ind = TechnicalIndicators.add_all_indicators(
            df,
            ema_fast=params.ema_fast,
            ema_slow=params.ema_slow,
            rsi_period=params.rsi_period,
            atr_period=params.atr_period
        )

        in_position = False
        entry_fill = 0.0
        entry_commission = 0.0
        entry_slippage = 0.0
        stop_loss = 0.0
        take_profit = 0.0
        cash = initial_capital          # realised capital
        trades: List[float] = []        # list of NET pnl fractions (after costs)
        cost_total = 0.0                # slippage + commission, summed over trades
        equity_curve: List[float] = []  # bar-by-bar marked-to-market equity
        curve_index: List[Any] = []     # timestamps aligned with equity_curve

        start = max(50, start_idx)
        end = end_idx if end_idx is not None else len(df_ind)
        for i in range(start, end):
            curr = df_ind.iloc[i]
            close = float(curr['Close'])
            high = float(curr['High'])
            low = float(curr['Low'])
            open_ = float(curr.get('Open', close))
            if pd.isna(open_):
                open_ = close

            if not in_position:
                # Ask the live strategy what it would do on this bar.
                window = df_ind.iloc[max(0, i - SIGNAL_WINDOW_BARS + 1): i + 1]
                signal = strategy.signal_from_indicators(symbol, window)

                if signal.action == "BUY" and signal.confidence >= MIN_ENTRY_CONFIDENCE:
                    entry_fill = costs.buy_fill(close)
                    entry_slippage = entry_fill / close - 1.0
                    entry_commission = costs.commission_pct(entry_fill)
                    cash *= (1.0 - entry_commission)
                    in_position = True
                    # The bracket the bot would submit, taken from the signal.
                    stop_loss = signal.stop_loss_price
                    take_profit = signal.take_profit_price
            else:
                exit_fill: Optional[float] = None
                exit_ref = 0.0
                # 1. Stop loss hit. A bar that opens below the stop gaps through
                #    it, so the stop-market order fills at the open, not the stop.
                if low <= stop_loss:
                    exit_ref = min(stop_loss, open_)
                    exit_fill = costs.sell_fill(exit_ref, market=True)
                # 2. Take profit hit (a resting limit order: fills at its limit)
                elif high >= take_profit:
                    exit_ref = take_profit
                    exit_fill = costs.sell_fill(exit_ref, market=False)
                # 3. Dynamic trend reversal exit (market order)
                elif str(curr['Regime']) == "TRENDING_BEAR" and float(curr['RSI']) > params.rsi_sell_threshold:
                    exit_ref = close
                    exit_fill = costs.sell_fill(exit_ref, market=True)

                if exit_fill is not None:
                    exit_commission = costs.commission_pct(exit_fill)
                    gross = (exit_fill - entry_fill) / entry_fill
                    leg_net = (1.0 + gross) * (1.0 - exit_commission)
                    cash *= leg_net
                    # Net return on the trade, including both commissions.
                    trades.append((1.0 - entry_commission) * leg_net - 1.0)
                    exit_slippage = (exit_ref - exit_fill) / exit_ref if exit_ref > 0 else 0.0
                    cost_total += entry_slippage + entry_commission + exit_slippage + exit_commission
                    in_position = False

            # Mark the portfolio to market on every bar. The Sharpe ratio is
            # computed from this curve, not from the handful of trade
            # returns, so it reflects what the equity actually did.
            #
            # The position is treated as a full-notional fraction of the
            # portfolio, so the unrealised move is converted from a price
            # difference into a portfolio return before it is added. The entry
            # commission is already in `cash`; the entry slippage shows up as
            # the gap between the fill and the bar's close.
            if in_position:
                unrealised = (close - entry_fill) / entry_fill
                equity_curve.append(cash * (1.0 + unrealised))
            else:
                equity_curve.append(cash)
            curve_index.append(df_ind.index[i])

        if not trades:
            return BacktestResult(
                total_trades=0, winning_trades=0, losing_trades=0,
                win_rate=0.0, total_pnl_pct=0.0, profit_factor=0.0,
                max_drawdown_pct=0.0, sharpe_ratio=0.0, fitness_score=-999.0
            )

        wins = [t for t in trades if t > 0]
        losses = [t for t in trades if t <= 0]
        win_rate = len(wins) / len(trades) if trades else 0.0

        total_gain = sum(wins)
        total_loss = abs(sum(losses))
        profit_factor = round(total_gain / (total_loss + 1e-9), 2) if total_loss > 0 else (5.0 if total_gain > 0 else 1.0)

        # Equity curve & Max Drawdown (measured bar by bar)
        peak = equity_curve[0]
        max_dd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd

        total_pnl_pct = (equity_curve[-1] - initial_capital) / initial_capital

        # Sharpe ratio from the DAILY equity curve.
        sharpe = StrategyBacktester._annualised_sharpe(equity_curve, pd.Index(curve_index))

        # Alpha (excess return over the benchmark over the same window). A
        # strategy that only rides the market has alpha ~ 0 and is not edge.
        # Without a benchmark the raw return is used, so a direct backtest
        # is unchanged.
        if benchmark_return_pct is not None:
            alpha_pct = total_pnl_pct - benchmark_return_pct
        else:
            alpha_pct = total_pnl_pct

        # Institutional Fitness Score:
        # Prioritizes Sharpe, Profit Factor, and Win Rate while heavily penalizing Drawdowns.
        # The return term is ALPHA, not raw return -- optimising raw return
        # rewards buying the market in a bull run and calls it skill.
        penalty_dd = max_dd * 3.0
        fitness = (sharpe * 0.4) + (profit_factor * 0.3) + (win_rate * 2.0) + (alpha_pct * 0.5) - penalty_dd

        # A handful of trades is not evidence. Penalise thin samples so the
        # optimizer does not rank a lucky two-trade run above a robust one.
        if len(trades) < Config.MIN_TRADES_TO_ADOPT:
            fitness -= (Config.MIN_TRADES_TO_ADOPT - len(trades)) * 0.5

        return BacktestResult(
            total_trades=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=round(win_rate * 100, 2),
            total_pnl_pct=round(total_pnl_pct * 100, 2),
            profit_factor=round(profit_factor, 2),
            max_drawdown_pct=round(max_dd * 100, 2),
            sharpe_ratio=round(float(sharpe), 2),
            fitness_score=round(float(fitness), 2),
            alpha_pct=round(float(alpha_pct) * 100, 2),
            cost_drag_pct=round(cost_total * 100, 3)
        )
