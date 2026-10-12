from dataclasses import dataclass
from typing import List, Dict, Any
import numpy as np
import pandas as pd
from strategy.adaptive_strategy import AdaptiveStrategy, StrategyParameters

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

class StrategyBacktester:
    """Fast vectorized & event backtester for parameter evaluation."""

    @staticmethod
    def run_backtest(
        df: pd.DataFrame,
        params: StrategyParameters,
        symbol: str = "TICKER",
        initial_capital: float = 10000.0
    ) -> BacktestResult:
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
        trades: List[float] = [] # list of pnl percentages

        for i in range(50, len(df_ind)):
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
                    in_position = False
                # 2. Take profit hit
                elif high >= take_profit:
                    pnl = (take_profit - entry_price) / entry_price
                    trades.append(pnl)
                    in_position = False
                # 3. Dynamic trend reversal exit
                elif str(curr['Regime']) == "TRENDING_BEAR" and float(curr['RSI']) > params.rsi_sell_threshold:
                    pnl = (close - entry_price) / entry_price
                    trades.append(pnl)
                    in_position = False

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

        # Equity curve & Max Drawdown
        equity_curve = [initial_capital]
        for t in trades:
            equity_curve.append(equity_curve[-1] * (1.0 + t))

        peak = equity_curve[0]
        max_dd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd

        total_pnl_pct = (equity_curve[-1] - initial_capital) / initial_capital

        # Annualized Sharpe approximation
        std_returns = np.std(trades) if len(trades) > 1 else 1e-9
        sharpe = (np.mean(trades) / (std_returns + 1e-9)) * np.sqrt(252) if std_returns > 0 else 0.0

        # Institutional Fitness Score:
        # Prioritizes Sharpe, Profit Factor, and Win Rate while heavily penalizing Drawdowns
        penalty_dd = max_dd * 3.0
        fitness = (sharpe * 0.4) + (profit_factor * 0.3) + (win_rate * 2.0) + (total_pnl_pct * 0.5) - penalty_dd

        # Filter out random flukes with less than 3 trades
        if len(trades) < 3:
            fitness -= 2.0

        return BacktestResult(
            total_trades=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=round(win_rate * 100, 2),
            total_pnl_pct=round(total_pnl_pct * 100, 2),
            profit_factor=round(profit_factor, 2),
            max_drawdown_pct=round(max_dd * 100, 2),
            sharpe_ratio=round(float(sharpe), 2),
            fitness_score=round(float(fitness), 2)
        )
