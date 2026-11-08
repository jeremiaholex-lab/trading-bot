import logging
from typing import Dict, List, Tuple, Optional
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

class PortfolioCorrelationManager:
    """
    Manages portfolio correlation risk to prevent high-beta co-movement.
    Avoids clustering multiple positions in assets whose return correlations
    exceed safe thresholds (e.g. > 0.75-0.80), which could otherwise trigger
    simultaneous stop-outs during market drawdowns.
    """

    def __init__(self, max_allowed_correlation: float = 0.75):
        self.max_allowed_correlation = max_allowed_correlation

    def calculate_pairwise_correlation(
        self,
        series_a: pd.Series,
        series_b: pd.Series,
        min_periods: int = 15
    ) -> float:
        """Computes Pearson correlation between two price or return series."""
        if len(series_a) < min_periods or len(series_b) < min_periods:
            return 0.0

        # Align and compute percent returns. Drop infinities too: zero or
        # near-zero prices can otherwise create non-finite returns.
        ret_a = series_a.pct_change().replace([np.inf, -np.inf], np.nan)
        ret_b = series_b.pct_change().replace([np.inf, -np.inf], np.nan)

        # Inner join on index
        df = pd.concat([ret_a, ret_b], axis=1).dropna()
        if len(df) < min_periods:
            return 0.0

        # Pearson correlation is undefined for a constant return series.
        # pandas/numpy emits RuntimeWarning in this case; preserve the existing
        # policy of treating undefined correlations as 0 without warning noise.
        if not np.isfinite(df.to_numpy(dtype=float)).all():
            return 0.0
        if (df.std(axis=0, ddof=0) <= 0).any():
            return 0.0

        corr = float(df.iloc[:, 0].corr(df.iloc[:, 1]))
        return 0.0 if np.isnan(corr) else corr

    def validate_candidate_correlation(
        self,
        candidate_symbol: str,
        candidate_series: pd.Series,
        existing_positions_series: Dict[str, pd.Series]
    ) -> Tuple[bool, str]:
        """
        Validates whether adding the candidate symbol exceeds the correlation ceiling
        with any existing open positions.
        """
        if not existing_positions_series:
            return True, "No open positions to correlate against"

        for sym, series in existing_positions_series.items():
            if sym == candidate_symbol:
                continue

            corr = self.calculate_pairwise_correlation(candidate_series, series)
            if corr >= self.max_allowed_correlation:
                return False, (
                    f"Excessive portfolio correlation with {sym} ({corr:.2f} >= {self.max_allowed_correlation:.2f}). "
                    f"Risk of simultaneous cluster drawdown."
                )

        return True, "Portfolio correlation within safe limits"
