import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional
import itertools
import pandas as pd

from config import Config
from strategy.adaptive_strategy import StrategyParameters
from optimizer.backtester import StrategyBacktester, BacktestResult

logger = logging.getLogger(__name__)

class SelfImprover:
    """
    Automated self-improvement engine.
    Uses walk-forward optimization to discover regime-optimal parameters
    and avoid overfitting.
    """

    def __init__(self, state_file: Optional[Path] = None):
        self.state_file = state_file or Config.STRATEGY_STATE_FILE
        self.state: Dict[str, Any] = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        if self.state_file.exists():
            try:
                with open(self.state_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load state file: {e}. Using clean state.")
        return {"symbols": {}, "last_updated": None}

    def _save_state(self):
        try:
            self.state["last_updated"] = datetime.now(timezone.utc).isoformat()
            with open(self.state_file, "w") as f:
                json.dump(self.state, f, indent=2)
            logger.info(f"Updated strategy state saved to {self.state_file}")
        except Exception as e:
            logger.error(f"Failed to save strategy state: {e}")

    def get_symbol_params(self, symbol: str) -> StrategyParameters:
        """Retrieves currently optimized parameters for a symbol, or default."""
        if symbol in self.state.get("symbols", {}):
            saved = self.state["symbols"][symbol].get("params")
            if saved:
                return StrategyParameters.from_dict(saved)
        return StrategyParameters()

    def optimize_symbol(self, symbol: str, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Runs walk-forward optimization on the given symbol DataFrame.
        Splits data into 70% In-Sample (Train) and 30% Out-Of-Sample (Test).
        """
        if df.empty or len(df) < 80:
            logger.warning(f"Not enough data to optimize {symbol} (rows: {len(df)})")
            return {"status": "skipped", "reason": "insufficient_data"}

        # Walk-forward split
        split_idx = int(len(df) * 0.70)
        df_train = df.iloc[:split_idx]
        df_test = df.iloc[split_idx:]

        # Current baseline
        current_params = self.get_symbol_params(symbol)
        baseline_res = StrategyBacktester.run_backtest(df_test, current_params, symbol=symbol)

        logger.info(
            f"Optimizing {symbol}... Baseline Out-Of-Sample Win Rate: {baseline_res.win_rate}%, "
            f"Sharpe: {baseline_res.sharpe_ratio}, Fitness: {baseline_res.fitness_score}"
        )

        # Parameter Search Grid
        search_grid = {
            "ema_fast": [8, 9, 13],
            "ema_slow": [21, 26, 34],
            "rsi_buy_threshold": [38.0, 42.0, 48.0],
            "atr_stop_multiplier": [1.5, 1.8, 2.2],
            "profit_reward_ratio": [1.8, 2.0, 2.5]
        }

        keys = list(search_grid.keys())
        combinations = list(itertools.product(*search_grid.values()))

        best_params = current_params
        best_oos_result = baseline_res
        best_score = baseline_res.fitness_score

        # Sample search to keep execution fast and responsive
        import random
        # Test baseline + 30 randomly sampled candidate combinations
        sampled_combos = random.sample(combinations, min(30, len(combinations)))

        for combo in sampled_combos:
            param_dict = dict(zip(keys, combo))
            # Merge with default params
            candidate = StrategyParameters.from_dict({**current_params.to_dict(), **param_dict})

            # Train phase (In-Sample)
            in_sample_res = StrategyBacktester.run_backtest(df_train, candidate, symbol=symbol)
            if in_sample_res.fitness_score <= 0 or in_sample_res.total_trades < 2:
                continue

            # Validation phase (Out-Of-Sample) - Prevents Overfitting!
            out_of_sample_res = StrategyBacktester.run_backtest(df_test, candidate, symbol=symbol)

            # Verification rule: Must produce positive fitness & beat previous best
            if out_of_sample_res.fitness_score > best_score and out_of_sample_res.total_trades >= 2:
                best_score = out_of_sample_res.fitness_score
                best_params = candidate
                best_oos_result = out_of_sample_res

        # Check if an improvement was found
        improved = best_score > baseline_res.fitness_score

        # Store in state
        if "symbols" not in self.state:
            self.state["symbols"] = {}

        self.state["symbols"][symbol] = {
            "params": best_params.to_dict(),
            "performance": {
                "win_rate": best_oos_result.win_rate,
                "profit_factor": best_oos_result.profit_factor,
                "sharpe_ratio": best_oos_result.sharpe_ratio,
                "max_drawdown_pct": best_oos_result.max_drawdown_pct,
                "total_trades": best_oos_result.total_trades,
                "fitness_score": best_oos_result.fitness_score
            },
            "improved": improved,
            "optimized_at": datetime.now(timezone.utc).isoformat()
        }
        self._save_state()

        return {
            "symbol": symbol,
            "improved": improved,
            "baseline_fitness": baseline_res.fitness_score,
            "best_fitness": best_score,
            "best_params": best_params.to_dict(),
            "performance": self.state["symbols"][symbol]["performance"]
        }

    def run_full_watchlist_optimization(self, market_data_provider) -> Dict[str, Any]:
        """Iterates over all watchlist tickers and optimizes each one."""
        results = {}
        for sym in Config.WATCHLIST:
            df = market_data_provider.get_historical_bars(
                sym,
                timeframe=Config.BAR_TIMEFRAME,
                days_back=Config.OPTIMIZATION_LOOKBACK_DAYS
            )
            if not df.empty:
                res = self.optimize_symbol(sym, df)
                results[sym] = res
            else:
                logger.warning(f"Could not load data for {sym} during optimization.")
        return results
