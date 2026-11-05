import logging
from typing import Dict, Any, Optional, Tuple
import pandas as pd
from indicators.technicals import TechnicalIndicators

logger = logging.getLogger(__name__)

class MeanReversionSleeve:
    """
    Sub-strategy designed specifically for RANGING and CHOPPY market regimes.
    Captures Bollinger Band and VWAP squeezes and mean-reversion bounces
    when trend-following models underperform due to lack of directional momentum.
    """

    def __init__(
        self,
        bb_std: float = 2.0,
        rsi_oversold_threshold: float = 35.0,
        rsi_overbought_threshold: float = 65.0,
        target_reward_ratio: float = 1.5
    ):
        self.bb_std = bb_std
        self.rsi_oversold_threshold = rsi_oversold_threshold
        self.rsi_overbought_threshold = rsi_overbought_threshold
        self.target_reward_ratio = target_reward_ratio

    def evaluate_mean_reversion_setup(
        self,
        symbol: str,
        df_ind: pd.DataFrame,
        atr: float
    ) -> Tuple[str, float, str, Dict[str, Any]]:
        """
        Analyzes the latest bar for high-probability mean-reversion setups.
        Returns (action, confidence, reason, metadata).
        """
        if df_ind.empty or len(df_ind) < 25:
            return "HOLD", 0.0, "Insufficient data bars", {}

        curr = df_ind.iloc[-1]
        prev = df_ind.iloc[-2]

        close = float(curr["Close"])
        prev_close = float(prev["Close"])
        rsi = float(curr["RSI"])
        bb_lower = float(curr["BB_Lower"])
        bb_upper = float(curr["BB_Upper"])
        bb_mid = float(curr["BB_Mid"])
        vwap = float(curr.get("VWAP", bb_mid))
        bb_width = float(curr.get("BB_Width", 0.04))

        # Check for Mean-Reversion Buy (Support at Lower Band + Oversold Bounce)
        touching_lower = close <= (bb_lower * 1.01)
        bouncing_up = close > prev_close
        rsi_oversold = rsi <= self.rsi_oversold_threshold

        if touching_lower and rsi_oversold and bouncing_up:
            confidence = 0.72
            if close < vwap:
                confidence += 0.08  # Discount relative to VWAP
            confidence = min(confidence, 0.88)
            reason = (
                f"Mean-Reversion Sleeve: Lower BB bounce (${close:.2f} <= ${bb_lower*1.01:.2f}) "
                f"with oversold RSI {rsi:.1f} targeting VWAP/Mid-Band (${vwap:.2f})"
            )
            return "BUY", confidence, reason, {
                "sleeve": "MEAN_REVERSION",
                "bb_lower": bb_lower,
                "bb_mid": bb_mid,
                "vwap": vwap,
                "rsi": rsi,
                "bb_width": bb_width
            }

        # Check for Mean-Reversion Exit (Overbought near Upper Band and elevated RSI)
        # Avoid firing on flat/constant series where bb_upper == bb_lower == close
        bands_differentiated = (bb_upper - bb_lower) > (close * 0.002)
        touching_upper = bands_differentiated and (close >= (bb_upper * 0.99))
        rsi_overbought = rsi >= self.rsi_overbought_threshold
        if (touching_upper and rsi >= 60.0) or rsi_overbought:
            return "SELL", 0.70, f"Mean-Reversion Sleeve: Upper BB exhaustion (RSI {rsi:.1f})", {"sleeve": "MEAN_REVERSION"}

        return "HOLD", 0.0, "No mean-reversion trigger", {}
