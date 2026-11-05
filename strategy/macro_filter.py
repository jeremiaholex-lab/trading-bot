import logging
from typing import Dict, Any, Optional, Tuple, List
import pandas as pd
from config import Config
from indicators.technicals import TechnicalIndicators
from .breadth_filter import MarketBreadthFilter

logger = logging.getLogger(__name__)

class MacroTrendFilter:
    """
    Evaluates higher-timeframe (Daily) market breadth across major benchmarks (SPY, QQQ)
    to protect against dead-cat bounce traps during broader market corrections.
    """

    def __init__(
        self,
        benchmark_symbols: Optional[List[str]] = None,
        breadth_filter: Optional[MarketBreadthFilter] = None
    ):
        self.benchmark_symbols = benchmark_symbols or Config.MACRO_BENCHMARK_SYMBOLS
        self.breadth_filter = breadth_filter or MarketBreadthFilter()
        self._cached_macro_state: Optional[Dict[str, Any]] = None

    def evaluate_macro_environment(self, market_data_provider) -> Dict[str, Any]:
        """
        Fetches Daily bars for benchmark symbols, calculates EMA structure and regime,
        and determines if the macro market is in a bearish/corrective condition.
        """
        details = {}
        bearish_benchmarks = []

        for b_sym in self.benchmark_symbols:
            try:
                df = market_data_provider.get_historical_bars(
                    b_sym, timeframe="1Day", days_back=60
                )
                if df.empty or len(df) < 25:
                    continue

                df_ind = TechnicalIndicators.add_all_indicators(df, ema_fast=9, ema_slow=21)
                last = df_ind.iloc[-1]
                close = float(last["Close"])
                ema_fast = float(last.get("EMA_9", close))
                ema_slow = float(last.get("EMA_21", close))
                regime = str(last.get("Regime", "UNKNOWN"))
                rsi = float(last.get("RSI", 50.0))

                is_benchmark_bear = regime == "TRENDING_BEAR" or close < ema_slow

                details[b_sym] = {
                    "close": close,
                    "ema_9": ema_fast,
                    "ema_21": ema_slow,
                    "regime": regime,
                    "rsi": rsi,
                    "is_bearish": is_benchmark_bear,
                }

                if is_benchmark_bear:
                    bearish_benchmarks.append(b_sym)
            except Exception as e:
                logger.warning(f"Could not compute macro trend for benchmark {b_sym}: {e}")

        is_macro_bearish = len(bearish_benchmarks) > 0
        state = {
            "is_bearish": is_macro_bearish,
            "bearish_benchmarks": bearish_benchmarks,
            "details": details,
        }
        self._cached_macro_state = state
        return state

    def validate_candidate_long(
        self,
        symbol: str,
        daily_stock_df: Optional[pd.DataFrame],
        macro_state: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, str]:
        """
        Validates whether a 15-minute long entry is permissible given the macro environment.
        - If market breadth is unhealthy (< min participation), new long positions are gated to prevent bull-trap entries.
        - If the broad market is in a Daily downtrend, long trades are blocked UNLESS the
          stock exhibits strong relative strength (holding above its own Daily 21 EMA).
        """
        if not Config.ENABLE_MACRO_TREND_FILTER:
            return True, "Macro filter disabled"

        state = macro_state or self._cached_macro_state
        inverse_etfs = getattr(Config, "INVERSE_ETFS", ["SH", "PSQ"])

        # Market breadth gate
        if state and "breadth" in state:
            breadth = state["breadth"]
            if not breadth.get("healthy_breadth", True) and symbol not in inverse_etfs:
                return False, f"Macro Breadth Gate: {breadth.get('reason')}"

        if not state or not state.get("is_bearish"):
            if symbol in inverse_etfs:
                return False, f"Macro Filter: Inverse ETF {symbol} blocked because market is bullish/neutral"
            return True, "Macro market conditions favorable"

        bearish_benchmarks = state.get("bearish_benchmarks", [])
        benchmarks_str = ", ".join(bearish_benchmarks)

        inverse_etfs = getattr(Config, "INVERSE_ETFS", ["SH", "PSQ"])
        if symbol in inverse_etfs:
            return True, f"Inverse ETF aligned with macro downtrend ({benchmarks_str})"

        # Direct benchmark trade in bear market
        if symbol in self.benchmark_symbols:
            return False, f"Macro Filter: Benchmark {symbol} is in Daily downtrend."

        # Check candidate's Daily relative strength
        if daily_stock_df is not None and not daily_stock_df.empty and len(daily_stock_df) >= 25:
            try:
                df_ind = TechnicalIndicators.add_all_indicators(daily_stock_df, ema_fast=9, ema_slow=21)
                last = df_ind.iloc[-1]
                close = float(last["Close"])
                ema_21 = float(last.get("EMA_21", close))
                regime = str(last.get("Regime", "UNKNOWN"))

                # If stock is also in a downtrend below its Daily 21 EMA -> Dead-cat bounce
                if close < ema_21 or regime == "TRENDING_BEAR":
                    return False, (
                        f"Macro Trend Gate: Market ({benchmarks_str}) is in Daily TRENDING_BEAR "
                        f"and {symbol} is below its Daily 21 EMA (${close:.2f} < ${ema_21:.2f})."
                    )
                else:
                    return True, f"Relative strength confirmed ({symbol} holding above Daily 21 EMA)"
            except Exception as e:
                logger.debug(f"Failed to calculate daily relative strength for {symbol}: {e}")

        # If daily data is missing, allow by default
        return True, "Daily trend data unavailable; macro check bypassed"
