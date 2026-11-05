import logging
from typing import List, Dict, Any, Optional
import pandas as pd
from indicators.technicals import TechnicalIndicators

logger = logging.getLogger(__name__)

class DynamicUniverseScreener:
    """
    Screens candidate symbols to select in-play, high-alpha trading opportunities.
    Evaluates:
      1. Relative Volume (RVOL) — identifies institutional participation.
      2. Volatility Expansion (ATR %) — filters for symbols with sufficient expansion potential.
      3. Trend Quality & Clean Price Structure — avoids dead/illiquid chop.
      4. Dollar Volume Liquidity — ensures entries/exits have minimal slippage.
    """

    def __init__(
        self,
        min_rvol: float = 1.2,
        min_atr_pct: float = 0.015,
        min_dollar_volume: float = 10_000_000.0,
        max_symbols_to_select: int = 5
    ):
        self.min_rvol = min_rvol
        self.min_atr_pct = min_atr_pct
        self.min_dollar_volume = min_dollar_volume
        self.max_symbols_to_select = max_symbols_to_select

    def screen_candidates(
        self,
        frames: Dict[str, pd.DataFrame]
    ) -> List[Dict[str, Any]]:
        """
        Takes a mapping of symbol -> historical bars DataFrame, computes in-play
        metrics, and returns sorted qualifying candidates.
        """
        qualifying = []

        for sym, df in frames.items():
            if df is None or df.empty or len(df) < 30:
                continue

            try:
                # Standardize column naming
                df_clean = df.copy()
                rename_map = {c: c.capitalize() for c in df_clean.columns}
                df_clean.rename(columns=rename_map, inplace=True)

                close = float(df_clean['Close'].iloc[-1])
                volume = float(df_clean['Volume'].iloc[-1])
                avg_vol = float(df_clean['Volume'].tail(20).mean()) + 1e-9
                rvol = volume / avg_vol

                # Calculate ATR percentage
                high = df_clean['High']
                low = df_clean['Low']
                tr1 = high - low
                tr2 = (high - df_clean['Close'].shift(1)).abs()
                tr3 = (low - df_clean['Close'].shift(1)).abs()
                tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                atr = float(tr.tail(14).mean())
                atr_pct = atr / close if close > 0 else 0.0

                # Approximate daily / bar dollar volume
                dollar_volume = close * avg_vol

                # Liquidity filter
                if dollar_volume < self.min_dollar_volume:
                    continue

                # Regime & Momentum alignment
                df_ind = TechnicalIndicators.add_all_indicators(df_clean)
                regime = str(df_ind['Regime'].iloc[-1])
                rsi = float(df_ind['RSI'].iloc[-1])

                # Compute Alpha Score (Composite of RVOL, Volatility, and Regime Alignment)
                base_score = (rvol * 35.0) + (atr_pct * 1000.0)

                if regime == "TRENDING_BULL":
                    base_score += 25.0
                elif regime == "RANGING":
                    base_score += 10.0
                elif regime == "HIGH_VOLATILITY":
                    base_score -= 20.0

                if 40.0 <= rsi <= 60.0:
                    base_score += 15.0  # Favorable pullback/breakout readiness

                # Only candidates meeting threshold requirements qualify
                if rvol >= (self.min_rvol - 1e-6) and atr_pct >= self.min_atr_pct:
                    qualifying.append({
                        "symbol": sym,
                        "score": round(base_score, 2),
                        "rvol": round(rvol, 2),
                        "atr_pct": round(atr_pct * 100, 2),
                        "regime": regime,
                        "close": round(close, 2),
                        "dollar_volume": round(dollar_volume, 0)
                    })

            except Exception as e:
                logger.debug(f"Error screening {sym}: {e}")
                continue

        # Sort descending by score
        qualifying.sort(key=lambda x: x["score"], reverse=True)
        return qualifying[:self.max_symbols_to_select]
