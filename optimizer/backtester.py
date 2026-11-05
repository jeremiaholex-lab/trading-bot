from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any, Callable, TypeVar
import numpy as np
import pandas as pd
from config import Config
from strategy.adaptive_strategy import AdaptiveStrategy, StrategyParameters

T = TypeVar("T")

# A Sharpe ratio above this is not a measure of edge -- it is an artefact of
# a tiny sample. Real money managers do not sustain Sharpe ratios above ~3.
MAX_PLAUSIBLE_SHARPE = 5.0


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
        benchmark_return_pct: Optional[float] = None
    ) -> BacktestResult:
        """Run the strategy over a window of an indicator-enriched frame.

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
        entry_price = 0.0
        stop_loss = 0.0
        take_profit = 0.0
        cash = initial_capital          # realised capital
        trades: List[float] = []        # list of pnl percentages
        equity_curve: List[float] = []  # bar-by-bar marked-to-market equity
        curve_index: List[Any] = []     # timestamps aligned with equity_curve

        start = max(50, start_idx)
        end = end_idx if end_idx is not None else len(df_ind)
        for i in range(start, end):
            curr = df_ind.iloc[i]
            prev = df_ind.iloc[i-1]
            close = float(curr['Close'])
            high = float(curr['High'])
            low = float(curr['Low'])

            if not in_position:
                # Check for entry signal directly using precomputed indicators
                regime = str(curr['Regime'])
                rsi = float(curr['RSI'])
                ema_f = float(curr[f"EMA_{params.ema_fast}"])
                ema_s = float(curr[f"EMA_{params.ema_slow}"])
                prev_f = float(prev[f"EMA_{params.ema_fast}"])
                prev_s = float(prev[f"EMA_{params.ema_slow}"])
                macd_h = float(curr['MACD_Hist'])
                prev_macd_h = float(prev['MACD_Hist'])
                vol_ratio = float(curr['Volume_Ratio'])
                atr_val = float(curr['ATR']) if not pd.isna(curr['ATR']) and curr['ATR'] > 0 else (close * 0.015)

                is_buy = False
                if regime == "TRENDING_BULL":
                    pullback_dip = (rsi <= params.rsi_buy_threshold + 5) and (rsi >= 30) and (macd_h > -0.05 or macd_h > prev_macd_h)
                    ema_cross = (prev_f <= prev_s) and (ema_f > ema_s) and (vol_ratio >= params.min_volume_ratio)
                    if pullback_dip or ema_cross:
                        is_buy = True
                elif regime == "RANGING":
                    bb_low = float(curr['BB_Lower'])
                    if close <= (bb_low * 1.01) and rsi <= 35 and close > prev['Close']:
                        is_buy = True

                if is_buy:
                    in_position = True
                    entry_price = close
                    risk_amount = atr_val * params.atr_stop_multiplier
                    stop_loss = max(close - risk_amount, 0.01)
                    take_profit = close + (risk_amount * params.profit_reward_ratio)
            else:
                # Check for exit conditions
                # 1. Stop loss hit
                if low <= stop_loss:
                    pnl = (stop_loss - entry_price) / entry_price
                    trades.append(pnl)
                    cash *= (1.0 + pnl)
                    in_position = False
                # 2. Take profit hit
                elif high >= take_profit:
                    pnl = (take_profit - entry_price) / entry_price
                    trades.append(pnl)
                    cash *= (1.0 + pnl)
                    in_position = False
                # 3. Dynamic trend reversal exit
                elif str(curr['Regime']) == "TRENDING_BEAR" and float(curr['RSI']) > params.rsi_sell_threshold:
                    pnl = (close - entry_price) / entry_price
                    trades.append(pnl)
                    cash *= (1.0 + pnl)
                    in_position = False

            # Mark the portfolio to market on every bar. The Sharpe ratio is
            # computed from this curve, not from the handful of trade
            # returns, so it reflects what the equity actually did.
            #
            # The position is treated as a full-notional fraction of the
            # portfolio, so the unrealised move is converted from a price
            # difference into a portfolio return before it is added.
            if in_position:
                unrealised = (close - entry_price) / entry_price
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
            alpha_pct=round(float(alpha_pct) * 100, 2)
        )
