import json
import logging
import random
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
        default_state = {"version": 1, "symbols": {}, "last_updated": None}
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        if "version" not in data:
                            data["version"] = 1
                        if "symbols" not in data:
                            data["symbols"] = {}
                        return data
            except Exception as e:
                logger.warning(f"Corrupted or invalid state file ({self.state_file}): {e}. Creating backup and using clean state.")
                try:
                    corrupt_backup = self.state_file.with_name(f"{self.state_file.stem}_corrupted_{int(datetime.now().timestamp())}.json")
                    self.state_file.rename(corrupt_backup)
                    logger.info(f"Backed up corrupted state file to {corrupt_backup}")
                except Exception as b_err:
                    logger.error(f"Failed to rename corrupted state file: {b_err}")
        return default_state

    def _save_state(self):
        """Atomically saves state to prevent corruption on sudden termination."""
        try:
            self.state["version"] = 1
            self.state["last_updated"] = datetime.now(timezone.utc).isoformat()
            tmp_file = self.state_file.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
                f.flush()
            tmp_file.replace(self.state_file)
            logger.info(f"Updated strategy state atomically saved to {self.state_file}")
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

        The held-out slice is scored ONCE, on the single candidate that won
        the training phase. Selecting candidates by their held-out score
        would turn the test set into a second training set and make every
        reported metric optimistic.
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

        # --- TRAIN PHASE -------------------------------------------------
        # Rank candidates purely on in-sample performance. The held-out slice
        # plays no part in this ranking.
        rng = random.Random(Config.OPTIMIZER_SEED)
        sampled_combos = rng.sample(combinations, min(30, len(combinations)))

        best_train_params = current_params
        best_train_fitness = float("-inf")
        best_train_trades = 0

        for combo in sampled_combos:
            param_dict = dict(zip(keys, combo))
            candidate = StrategyParameters.from_dict({**current_params.to_dict(), **param_dict})

            in_sample_res = StrategyBacktester.run_backtest(df_train, candidate, symbol=symbol)
            if in_sample_res.fitness_score <= 0 or in_sample_res.total_trades < 2:
                continue

            if in_sample_res.fitness_score > best_train_fitness:
                best_train_fitness = in_sample_res.fitness_score
                best_train_params = candidate
                best_train_trades = in_sample_res.total_trades

        # If nothing cleared the in-sample gates, keep the current parameters.
        if best_train_fitness == float("-inf"):
            logger.info(
                f"{symbol}: no candidate cleared the in-sample gates; keeping current parameters."
            )
            best_params = current_params
            out_of_sample_res = baseline_res
        else:
            # --- VALIDATION PHASE --------------------------------------
            # The held-out slice is scored exactly once, on the training
            # winner. This is the only time df_test is consulted.
            out_of_sample_res = StrategyBacktester.run_backtest(df_test, best_train_params, symbol=symbol)
            best_params = best_train_params

        # --- ADOPTION GATES ---------------------------------------------
        # A candidate must be profitable out of sample, beat the baseline by
        # a material margin, AND be supported by enough trades to be more
        # than noise.
        #
        # The margin matters as much as the floor. A rolling-origin
        # calibration over 311 (symbol, origin) pairs showed that a
        # candidate's ABSOLUTE fitness does not reliably predict whether it
        # goes on to work -- candidates scoring above 3.0 did no better than
        # the defaults. What predicted a good outcome was the SIZE OF THE
        # IMPROVEMENT over the parameters already in use.
        margin = out_of_sample_res.fitness_score - baseline_res.fitness_score

        passes_floor = (
            out_of_sample_res.fitness_score >= Config.MIN_FITNESS_TO_ADOPT
            and out_of_sample_res.total_trades >= Config.MIN_TRADES_TO_ADOPT
        )
        beats_baseline = margin >= Config.MIN_FITNESS_IMPROVEMENT
        improved = bool(passes_floor and beats_baseline)

        if not improved:
            logger.info(
                f"{symbol}: keeping current parameters (candidate fitness "
                f"{out_of_sample_res.fitness_score} over {out_of_sample_res.total_trades} trades, "
                f"margin {margin:+.2f} vs baseline {baseline_res.fitness_score}; "
                f"needs fitness >= {Config.MIN_FITNESS_TO_ADOPT}, "
                f"margin >= {Config.MIN_FITNESS_IMPROVEMENT}, "
                f"and >= {Config.MIN_TRADES_TO_ADOPT} trades)."
            )
            # Fall back to the current parameters, but report the *measured*
            # performance of those parameters on the held-out slice.
            best_params = current_params
            out_of_sample_res = baseline_res

        # Store in state
        if "symbols" not in self.state:
            self.state["symbols"] = {}

        self.state["symbols"][symbol] = {
            "params": best_params.to_dict(),
            "performance": {
                "win_rate": out_of_sample_res.win_rate,
                "profit_factor": out_of_sample_res.profit_factor,
                "sharpe_ratio": out_of_sample_res.sharpe_ratio,
                "max_drawdown_pct": out_of_sample_res.max_drawdown_pct,
                "total_trades": out_of_sample_res.total_trades,
                "fitness_score": out_of_sample_res.fitness_score
            },
            "improved": improved,
            "optimized_at": datetime.now(timezone.utc).isoformat()
        }
        self._save_state()

        return {
            "symbol": symbol,
            "improved": improved,
            "baseline_fitness": baseline_res.fitness_score,
            "best_fitness": out_of_sample_res.fitness_score,
            "fitness_margin": round(margin, 2),
            "best_params": best_params.to_dict(),
            "performance": self.state["symbols"][symbol]["performance"]
        }

    def run_full_watchlist_optimization(self, market_data_provider) -> Dict[str, Any]:
        """Iterates over all watchlist tickers and optimizes each one."""
        results = {}
        for sym in Config.WATCHLIST:
            df = market_data_provider.get_historical_bars(
                sym,
                timeframe=Config.OPTIMIZATION_TIMEFRAME,
                days_back=Config.OPTIMIZATION_LOOKBACK_DAYS
            )
            if not df.empty:
                res = self.optimize_symbol(sym, df)
                results[sym] = res
            else:
                logger.warning(f"Could not load data for {sym} during optimization.")
        return results
