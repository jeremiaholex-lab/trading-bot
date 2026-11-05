from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any
import pandas as pd
from indicators.technicals import TechnicalIndicators
from .mean_reversion_sleeve import MeanReversionSleeve

@dataclass
class TradeSignal:
    action: str              # 'BUY', 'SELL', 'HOLD'
    symbol: str
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    confidence: float        # 0.0 to 1.0
    regime: str              # Market regime string
    reason: str              # Descriptive explanation of signal rationale
    metadata: Dict[str, Any]

@dataclass
class StrategyParameters:
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    rsi_buy_threshold: float = 42.0     # Buy pullbacks when RSI is below this in bull trend
    rsi_sell_threshold: float = 70.0    # Overbought exit
    atr_period: int = 14
    atr_stop_multiplier: float = 1.8   # Stop loss = Entry - (ATR * multiplier)
    profit_reward_ratio: float = 2.0   # Take profit = Entry + (Risk * ratio)
    min_volume_ratio: float = 0.8      # Minimum volume compared to 20 SMA

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict):
        # Filter only valid fields
        valid_keys = cls.__dataclass_fields__.keys()
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


class AdaptiveStrategy:
    """
    Regime-aware quantitative strategy designed for asymmetric risk/reward.
    Adapts parameters dynamically based on market volatility and feedback loops.
    """

    def __init__(
        self,
        params: Optional[StrategyParameters] = None,
        mean_reversion_sleeve: Optional[MeanReversionSleeve] = None
    ):
        self.params = params or StrategyParameters()
        self.mean_reversion_sleeve = mean_reversion_sleeve or MeanReversionSleeve()

    def update_params(self, new_params: StrategyParameters):
        self.params = new_params

    def generate_signal(self, symbol: str, df: pd.DataFrame) -> TradeSignal:
        """
        Analyzes the latest market bars and outputs a high-conviction TradeSignal.
        """
        if df.empty or len(df) < 50:
            return TradeSignal(
                action="HOLD",
                symbol=symbol,
                entry_price=0.0,
                stop_loss_price=0.0,
                take_profit_price=0.0,
                confidence=0.0,
                regime="UNKNOWN",
                reason="Insufficient data bars (need at least 50)",
                metadata={}
            )

        # Calculate all indicators
        df_ind = TechnicalIndicators.add_all_indicators(
            df,
            ema_fast=self.params.ema_fast,
            ema_slow=self.params.ema_slow,
            rsi_period=self.params.rsi_period,
            atr_period=self.params.atr_period
        )

        curr = df_ind.iloc[-1]
        prev = df_ind.iloc[-2]

        close = float(curr['Close'])
        atr = float(curr['ATR'])
        regime = str(curr['Regime'])
        rsi = float(curr['RSI'])
        ema_fast = float(curr[f"EMA_{self.params.ema_fast}"])
        ema_slow = float(curr[f"EMA_{self.params.ema_slow}"])
        macd_hist = float(curr['MACD_Hist'])
        volume_ratio = float(curr['Volume_Ratio'])

        # Fallback if ATR is NaN
        if pd.isna(atr) or atr <= 0:
            atr = close * 0.015

        # Signal evaluation
        action = "HOLD"
        confidence = 0.0
        reasons = []

        # -------------------------------------------------------------
        # 1. High Volatility Regime: Protect Capital (Stay Cautious)
        # -------------------------------------------------------------
        if regime == "HIGH_VOLATILITY":
            return TradeSignal(
                action="HOLD",
                symbol=symbol,
                entry_price=close,
                stop_loss_price=close - (atr * self.params.atr_stop_multiplier),
                take_profit_price=close + (atr * self.params.atr_stop_multiplier * self.params.profit_reward_ratio),
                confidence=0.0,
                regime=regime,
                reason="High market volatility spike detected. Preserving capital.",
                metadata={"rsi": rsi, "atr": atr}
            )

        # -------------------------------------------------------------
        # 2. Bullish Trend: Buy Pullbacks & Trend Momentum
        # -------------------------------------------------------------
        if regime == "TRENDING_BULL":
            # Condition A: Trend pullback entry (RSI pulled back to reasonable level, now turning up)
            pullback_dip = (rsi <= self.params.rsi_buy_threshold + 5) and (rsi >= 30)
            ema_bounce = close >= (ema_slow * 0.995)
            momentum_turning = macd_hist > -0.05 or macd_hist > prev['MACD_Hist']

            # Condition B: Bullish momentum breakout
            ema_cross = (prev[f"EMA_{self.params.ema_fast}"] <= prev[f"EMA_{self.params.ema_slow}"]) and (ema_fast > ema_slow)

            if (pullback_dip and ema_bounce and momentum_turning) or (ema_cross and volume_ratio >= self.params.min_volume_ratio):
                action = "BUY"
                score = 0.6
                if rsi < 45:
                    score += 0.15
                    reasons.append("Prime oversold dip in bull trend")
                if volume_ratio >= 1.2:
                    score += 0.15
                    reasons.append(f"Strong volume expansion ({volume_ratio:.2f}x avg)")
                if macd_hist > 0:
                    score += 0.10
                    reasons.append("Positive MACD acceleration")
                confidence = min(round(score, 2), 0.95)
                reasons.append(f"Bullish trend alignment (Fast EMA > Slow EMA)")

        # -------------------------------------------------------------
        # 3. Ranging Regime: Mean Reversion
        # -------------------------------------------------------------
        elif regime == "RANGING" or regime == "CHOPPY":
            if hasattr(self, "mean_reversion_sleeve") and self.mean_reversion_sleeve:
                mr_action, mr_conf, mr_reason, mr_meta = self.mean_reversion_sleeve.evaluate_mean_reversion_setup(
                    symbol=symbol,
                    df_ind=df_ind,
                    atr=atr
                )
                if mr_action != "HOLD":
                    action = mr_action
                    confidence = mr_conf
                    reasons.append(mr_reason)
            else:
                # Fallback basic mean reversion
                bb_lower = float(curr['BB_Lower'])
                at_support = close <= (bb_lower * 1.01)
                rsi_oversold = rsi <= 35
                if at_support and rsi_oversold and close > prev['Close']:
                    action = "BUY"
                    confidence = 0.70
                    reasons.append(f"Mean-reversion bounce at lower Bollinger Band (RSI: {rsi:.1f})")

        # -------------------------------------------------------------
        # 4. Bearish Trend: Avoid longs or Signal Exit
        # -------------------------------------------------------------
        elif regime == "TRENDING_BEAR":
            if rsi >= self.params.rsi_sell_threshold or (ema_fast < ema_slow and macd_hist < 0):
                action = "SELL"
                confidence = 0.75
                reasons.append("Bearish trend confirmed: downward momentum dominating")

        # Calculate disciplined bracket levels
        risk_per_share = atr * self.params.atr_stop_multiplier
        stop_loss = round(close - risk_per_share, 2)
        take_profit = round(close + (risk_per_share * self.params.profit_reward_ratio), 2)

        reason_str = " | ".join(reasons) if reasons else "No qualifying high-probability setup."

        return TradeSignal(
            action=action,
            symbol=symbol,
            entry_price=round(close, 2),
            stop_loss_price=max(stop_loss, 0.01),
            take_profit_price=take_profit,
            confidence=confidence,
            regime=regime,
            reason=reason_str,
            metadata={
                "rsi": round(rsi, 2),
                "atr": round(atr, 2),
                "ema_fast": round(ema_fast, 2),
                "ema_slow": round(ema_slow, 2),
                "volume_ratio": round(volume_ratio, 2)
            }
        )
