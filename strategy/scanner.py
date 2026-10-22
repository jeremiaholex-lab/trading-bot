import logging
import pandas as pd
from typing import List, Dict, Any, Optional
from indicators.technicals import TechnicalIndicators

logger = logging.getLogger("AssetScanner")

class AssetScanner:
    """
    Scans a multi-asset universe (e.g. SPY, QQQ, AAPL, MSFT, NVDA, TSLA, AMZN, GOOGL, META, etc.),
    downloads or accepts historical OHLCV data, computes regime/momentum metrics,
    and ranks them to select the top conviction trading opportunities.
    """

    def __init__(self, watchlist: List[str]):
        self.watchlist = [s.strip().upper() for s in watchlist if s.strip()]

    def scan_and_rank(self, market_data_map: Dict[str, pd.DataFrame]) -> List[Dict[str, Any]]:
        """
        Takes a dictionary mapping symbols to their historical OHLCV DataFrames,
        computes indicators, scores each asset, and returns a sorted list of ranked candidates.
        
        Scoring Criteria:
        - Trend alignment (TRENDING_BULL = high score)
        - Volume expansion (Volume Ratio > 1.0)
        - RSI health (pullbacks or strong momentum)
        - Volatility stability (lower ATR pct relative to market norm)
        """
        ranked_candidates = []

        for symbol, df in market_data_map.items():
            if df is None or df.empty or len(df) < 50:
                continue

            try:
                df_ind = TechnicalIndicators.add_all_indicators(df)
                curr = df_ind.iloc[-1]
                regime = str(curr.get('Regime', 'RANGING'))
                rsi = float(curr.get('RSI', 50.0))
                volume_ratio = float(curr.get('Volume_Ratio', 1.0))
                atr_pct = float(curr.get('ATR_Pct', 0.02))
                close = float(curr.get('Close', 0.0))
                vwap = float(curr.get('VWAP', close))

                # Base score calculation
                score = 0.0
                reasons = []

                if regime == "TRENDING_BULL":
                    score += 50.0
                    reasons.append("Trending Bull")
                    if close > vwap:
                        score += 15.0
                        reasons.append("Above VWAP")
                elif regime == "RANGING":
                    score += 25.0
                    reasons.append("Ranging (Mean-reversion)")
                elif regime == "HIGH_VOLATILITY":
                    score -= 30.0
                    reasons.append("High Volatility (Excluded)")
                elif regime == "TRENDING_BEAR":
                    score -= 20.0
                    reasons.append("Trending Bear")

                # Volume bonus
                if volume_ratio >= 1.2:
                    score += 15.0
                    reasons.append(f"Volume Spike ({volume_ratio:.2f}x)")

                # RSI pullback bonus for bulls
                if regime == "TRENDING_BULL" and 35 <= rsi <= 50:
                    score += 20.0
                    reasons.append(f"Bullish Pullback RSI ({rsi:.1f})")

                ranked_candidates.append({
                    "symbol": symbol,
                    "score": round(score, 2),
                    "regime": regime,
                    "rsi": round(rsi, 2),
                    "volume_ratio": round(volume_ratio, 2),
                    "close": round(close, 2),
                    "reasons": " | ".join(reasons),
                    "df": df_ind
                })
            except Exception as e:
                logger.error(f"Error scanning symbol {symbol}: {e}")

        # Sort by score descending (highest conviction first)
        ranked_candidates.sort(key=lambda x: x['score'], reverse=True)
        return ranked_candidates
