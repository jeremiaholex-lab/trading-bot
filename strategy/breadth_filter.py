import logging
from typing import Dict, List, Tuple, Optional, Any
import pandas as pd
from indicators.technicals import TechnicalIndicators

logger = logging.getLogger(__name__)

class MarketBreadthFilter:
    """
    Measures broad market health, participation ratio, and volatility term
    to prevent buying into exhausted or narrow-breadth market rallies.

    Metrics:
      1. Participation Ratio: Percentage of universe trading above their short/medium EMAs.
      2. Advance/Decline Momentum: Ratio of net advancing assets over past N bars.
      3. Trend Quality Confluence: Blocks long trades if broad participation falls below threshold.
    """

    def __init__(self, min_bullish_participation_pct: float = 0.40):
        self.min_bullish_participation_pct = min_bullish_participation_pct

    def compute_market_breadth(
        self,
        universe_frames: Dict[str, pd.DataFrame]
    ) -> Dict[str, Any]:
        """
        Computes aggregate market breadth across all available symbol frames.
        """
        total = len(universe_frames)
        if total == 0:
            return {
                "total_symbols": 0,
                "bullish_count": 0,
                "participation_pct": 1.0,
                "healthy_breadth": True,
                "reason": "No universe data provided"
            }

        bullish_count = 0

        for sym, df in universe_frames.items():
            if df is None or df.empty or len(df) < 25:
                continue

            try:
                df_clean = df.copy()
                rename_map = {c: c.capitalize() for c in df_clean.columns}
                df_clean.rename(columns=rename_map, inplace=True)

                close = float(df_clean['Close'].iloc[-1])
                ema_21 = float(df_clean['Close'].ewm(span=21, adjust=False).mean().iloc[-1])

                # Asset is considered healthy if above 21 EMA
                if close >= ema_21:
                    bullish_count += 1
            except Exception:
                continue

        participation = bullish_count / total if total > 0 else 0.0
        is_healthy = participation >= self.min_bullish_participation_pct

        return {
            "total_symbols": total,
            "bullish_count": bullish_count,
            "participation_pct": round(participation, 2),
            "healthy_breadth": is_healthy,
            "reason": (
                f"Breadth participation healthy ({participation*100:.1f}% >= {self.min_bullish_participation_pct*100:.0f}%)"
                if is_healthy
                else f"Poor market breadth: only {participation*100:.1f}% of universe above 21 EMA (< {self.min_bullish_participation_pct*100:.0f}%)"
            )
        }
