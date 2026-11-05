"""
Data Quality Gates for Market Data

Provides validation, anomaly detection, and quality scoring for OHLCV market data.
Detects gaps, outliers, stale quotes, and OHLC violations.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple, Any
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class QualitySeverity(Enum):
    """Severity levels for quality issues."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class QualityCheck(Enum):
    """Types of quality checks."""
    GAP_DETECTION = "gap_detection"
    OUTLIER_DETECTION = "outlier_detection"
    STALE_QUOTE = "stale_quote"
    OHLC_VIOLATION = "ohlc_violation"
    VOLUME_ANOMALY = "volume_anomaly"
    TIMESTAMP_ISSUE = "timestamp_issue"


@dataclass
class QualityIssue:
    """Represents a single data quality issue."""
    check: QualityCheck
    severity: QualitySeverity
    message: str
    row_index: Optional[int] = None
    timestamp: Optional[datetime] = None
    column: Optional[str] = None
    value: Optional[float] = None
    expected: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class QualityReport:
    """Aggregated quality report for a DataFrame."""
    symbol: str
    timeframe: str
    total_rows: int
    issues: List[QualityIssue] = field(default_factory=list)
    quality_score: float = 1.0  # 0.0 to 1.0
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add_issue(self, issue: QualityIssue):
        self.issues.append(issue)

    def has_critical(self) -> bool:
        return any(i.severity == QualitySeverity.CRITICAL for i in self.issues)

    def has_warnings(self) -> bool:
        return any(i.severity == QualitySeverity.WARNING for i in self.issues)

    def by_check(self, check: QualityCheck) -> List[QualityIssue]:
        return [i for i in self.issues if i.check == check]

    def should_skip(self, min_score: float = 0.6) -> Tuple[bool, str]:
        """Determines whether trading should be skipped for this symbol.
        Returns (should_skip, reason).
        """
        if self.has_critical():
            criticals = [i.message for i in self.issues if i.severity == QualitySeverity.CRITICAL]
            return True, f"Critical data quality issue: {'; '.join(criticals[:2])}"
        if self.quality_score < min_score:
            return True, f"Data quality score too low ({self.quality_score:.2f} < {min_score:.2f})"
        return False, ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "total_rows": self.total_rows,
            "quality_score": round(self.quality_score, 3),
            "critical_count": sum(1 for i in self.issues if i.severity == QualitySeverity.CRITICAL),
            "warning_count": sum(1 for i in self.issues if i.severity == QualitySeverity.WARNING),
            "info_count": sum(1 for i in self.issues if i.severity == QualitySeverity.INFO),
            "checked_at": self.checked_at.isoformat(),
            "issues": [
                {
                    "check": i.check.value,
                    "severity": i.severity.value,
                    "message": i.message,
                    "row_index": i.row_index,
                    "timestamp": i.timestamp.isoformat() if i.timestamp else None,
                    "column": i.column,
                    "value": i.value,
                    "expected": i.expected,
                    "metadata": i.metadata
                }
                for i in self.issues
            ],
            "metadata": self.metadata
        }


class DataQualityConfig:
    """Configuration for data quality thresholds."""

    # Gap detection
    MAX_GAP_MULTIPLIER: float = 3.0  # gap > expected_interval * multiplier
    MIN_BARS_FOR_GAP_DETECTION: int = 50

    # Outlier detection
    OUTLIER_ZSCORE_THRESHOLD: float = 4.0  # z-score threshold for price outliers
    OUTLIER_LOOKBACK_WINDOW: int = 50  # bars for rolling stats
    MIN_BARS_FOR_OUTLIER_DETECTION: int = 50

    # Stale quote detection
    MAX_STALE_BARS: int = 3  # consecutive bars with same close price
    STALE_PRICE_TOLERANCE: float = 1e-6  # tolerance for price equality
    MAX_STALENESS_SECONDS: int = 3600  # 1 hour max age for latest bar

    # OHLC validation
    ENABLE_OHLC_VALIDATION: bool = True

    # Volume anomaly
    VOLUME_SPIKE_MULTIPLIER: float = 10.0  # volume spike vs rolling median
    VOLUME_LOOKBACK: int = 20

    # Scoring weights
    SCORE_WEIGHTS: Dict[str, float] = {
        "gap": 0.25,
        "outlier": 0.20,
        "stale": 0.20,
        "ohlc": 0.20,
        "volume": 0.15,
    }


class DataQualityGate:
    """Main class for running data quality checks on OHLCV DataFrames."""

    def __init__(self, config: Optional[DataQualityConfig] = None):
        self.config = config or DataQualityConfig()

    def check_gaps(self, df: pd.DataFrame, timeframe: str) -> List[QualityIssue]:
        """Detect gaps in the time series based on expected bar intervals."""
        issues: List[QualityIssue] = []

        if len(df) < self.config.MIN_BARS_FOR_GAP_DETECTION:
            return issues

        # Expected interval in minutes
        interval_map = {
            "1Min": 1, "5Min": 5, "15Min": 15,
            "1Hour": 60, "1Day": 1440
        }
        expected_minutes = {"1Min": 1, "5Min": 5, "15Min": 15,
                           "1Hour": 60, "1Day": 1440}.get(timeframe, 15)

        # Ensure index is datetime
        idx = pd.to_datetime(df.index)
        diffs = idx.to_series().diff().dt.total_seconds() / 60  # minutes

        expected_gap = expected_minutes
        max_allowed_gap = expected_gap * self.config.MAX_GAP_MULTIPLIER

        for idx, (gap, ts) in enumerate(zip(diffs, idx)):
            if pd.isna(gap) or gap <= expected_gap * 1.1:
                continue
            if gap > max_allowed_gap:
                missing_bars = int(round(gap / expected_minutes)) - 1
                issues.append(QualityIssue(
                    check=QualityCheck.GAP_DETECTION,
                    severity=QualitySeverity.WARNING if gap < expected_gap * 5 else QualitySeverity.CRITICAL,
                    message=f"Gap of {gap:.0f} min ({missing_bars} missing bars) detected at {ts}",
                    row_index=idx,
                    timestamp=ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts,
                    column="index",
                    value=gap,
                    expected=expected_gap,
                    metadata={"missing_bars": missing_bars, "gap_minutes": gap}
                ))

        return issues

    def check_outliers(self, df: pd.DataFrame) -> List[QualityIssue]:
        """Detect price outliers using z-score on returns."""
        issues: List[QualityIssue] = []

        if len(df) < self.config.MIN_BARS_FOR_OUTLIER_DETECTION:
            return issues

        close = df['Close'].astype(float)
        returns = close.pct_change().dropna()

        if len(returns) < self.config.OUTLIER_LOOKBACK_WINDOW:
            return issues

        # Rolling z-score
        roll_mean = returns.rolling(self.config.OUTLIER_LOOKBACK_WINDOW, min_periods=20).mean()
        roll_std = returns.rolling(self.config.OUTLIER_LOOKBACK_WINDOW, min_periods=20).std()
        zscore = (returns - roll_mean) / (roll_std + 1e-12)

        threshold = self.config.OUTLIER_ZSCORE_THRESHOLD
        outliers = zscore.abs() > threshold

        for idx in outliers[outliers].index:
            ts = df.index[df.index.get_loc(idx)]
            issues.append(QualityIssue(
                check=QualityCheck.OUTLIER_DETECTION,
                severity=QualitySeverity.WARNING,
                message=f"Price return z-score {zscore.loc[idx]:.2f} exceeds threshold {self.config.OUTLIER_ZSCORE_THRESHOLD}",
                row_index=df.index.get_loc(idx),
                timestamp=ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts,
                column="Close",
                value=float(df.loc[idx, 'Close']),
                expected=None,
                metadata={"zscore": float(zscore.loc[idx]), "return": float(returns.loc[idx])}
            ))

        return issues

    def check_stale_quotes(self, df: pd.DataFrame, symbol: str) -> List[QualityIssue]:
        """Detect stale quotes (unchanged prices, old timestamps)."""
        issues: List[QualityIssue] = []

        if len(df) < self.config.MAX_STALE_BARS:
            return issues

        # Check for consecutive bars with identical close price
        close = df['Close'].astype(float)
        price_changes = close.diff().abs()
        stale_mask = price_changes <= self.config.STALE_PRICE_TOLERANCE

        # Find consecutive stale bars
        stale_groups = (stale_mask != stale_mask.shift()).cumsum()
        for _, group in stale_mask[stale_mask].groupby(stale_groups[stale_mask]):
            if len(group) >= self.config.MAX_STALE_BARS:
                first_idx = group.index[0]
                last_idx = group.index[-1]
                ts = df.index[df.index.get_loc(group.index[0])]
                issues.append(QualityIssue(
                    check=QualityCheck.STALE_QUOTE,
                    severity=QualitySeverity.WARNING,
                    message=f"Stale quote detected: {len(group)} consecutive bars with unchanged close price",
                    row_index=df.index.get_loc(last_idx),
                    timestamp=ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts,
                    column="Close",
                    value=float(df.loc[group.index[-1], 'Close']),
                    metadata={"stale_bars": len(group), "price": float(df.loc[group.index[-1], 'Close'])}
                ))

        # Check for stale timestamps (latest bar too old)
        if len(df) > 0:
            latest_ts = pd.to_datetime(df.index[-1])
            if hasattr(latest_ts, 'to_pydatetime'):
                latest_ts = latest_ts.to_pydatetime()
            if getattr(latest_ts, 'tzinfo', None) is None:
                latest_ts = latest_ts.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            age_seconds = (now - latest_ts).total_seconds()
            if age_seconds > self.config.MAX_STALENESS_SECONDS:
                issues.append(QualityIssue(
                    check=QualityCheck.STALE_QUOTE,
                    severity=QualitySeverity.WARNING,
                    message=f"Latest bar is {age_seconds/60:.0f} minutes old (max {self.config.MAX_STALENESS_SECONDS/60} min)",
                    timestamp=latest_ts.to_pydatetime() if hasattr(latest_ts, 'to_pydatetime') else latest_ts,
                    value=age_seconds,
                    expected=self.config.MAX_STALENESS_SECONDS,
                    metadata={"latest_bar_age_seconds": age_seconds}
                ))

        return issues

    def check_ohlc_violations(self, df: pd.DataFrame) -> List[QualityIssue]:
        """Validate OHLC relationships: High >= Low, Open/Close within High-Low."""
        issues: List[QualityIssue] = []

        if not self.config.ENABLE_OHLC_VALIDATION or len(df) == 0:
            return issues

        for idx, row in df.iterrows():
            o, h, l, c = row['Open'], row['High'], row['Low'], row['Close']

            # Check High >= Low
            if h < l:
                issues.append(QualityIssue(
                    check=QualityCheck.OHLC_VIOLATION,
                    severity=QualitySeverity.CRITICAL,
                    message=f"OHLC violation: High ({h}) < Low ({l})",
                    row_index=df.index.get_loc(idx),
                    timestamp=idx.to_pydatetime() if hasattr(idx, 'to_pydatetime') else idx,
                    column="High/Low",
                    value=float(h),
                    expected=float(l),
                    metadata={"high": float(h), "low": float(l)}
                ))

            # Check Open/Close within High-Low range
            for price, col in [(o, 'Open'), (c, 'Close')]:
                if pd.notna(price) and (price > h or price < l):
                    issues.append(QualityIssue(
                        check=QualityCheck.OHLC_VIOLATION,
                        severity=QualitySeverity.CRITICAL,
                        message=f"OHLC violation: {col} ({price}) outside High-Low range [{l}, {h}]",
                        row_index=df.index.get_loc(idx),
                        timestamp=idx.to_pydatetime() if hasattr(idx, 'to_pydatetime') else idx,
                        column=col,
                        value=float(price),
                        expected=f"[{float(l)}, {float(h)}]",
                        metadata={"high": float(h), "low": float(l)}
                    ))

        return issues

    def check_volume_anomalies(self, df: pd.DataFrame) -> List[QualityIssue]:
        """Detect volume spikes vs rolling median."""
        issues: List[QualityIssue] = []

        if len(df) < self.config.VOLUME_LOOKBACK:
            return issues

        volume = df['Volume'].astype(float)
        if volume.sum() == 0:
            return issues

        roll_median = volume.rolling(self.config.VOLUME_LOOKBACK, min_periods=5).median()
        roll_std = volume.rolling(self.config.VOLUME_LOOKBACK, min_periods=5).std()

        # Spike detection: volume > median * multiplier
        spike_mask = volume > (roll_median * self.config.VOLUME_SPIKE_MULTIPLIER)
        # Drop detection: volume drops to near zero while price moves
        drop_mask = (volume < roll_median * 0.01) & (df['Close'].pct_change().abs() > 0.01)

        for idx in df[spike_mask].index:
            ts = df.index[df.index.get_loc(idx)]
            issues.append(QualityIssue(
                check=QualityCheck.VOLUME_ANOMALY,
                severity=QualitySeverity.INFO,
                message=f"Volume spike: {df.loc[idx, 'Volume']:.0f} vs median {df.loc[idx, 'Volume'] / df['Volume'].rolling(20).median().loc[idx]:.0f}",
                row_index=df.index.get_loc(idx),
                timestamp=idx.to_pydatetime() if hasattr(idx, 'to_pydatetime') else idx,
                column="Volume",
                value=float(df.loc[idx, 'Volume']),
                metadata={"median_volume": float(df['Volume'].rolling(20).median().loc[idx])}
            ))

        for idx in df[drop_mask].index:
            ts = df.index[df.index.get_loc(idx)]
            issues.append(QualityIssue(
                check=QualityCheck.VOLUME_ANOMALY,
                severity=QualitySeverity.WARNING,
                message=f"Volume drop with price move: vol={df.loc[idx, 'Volume']:.0f} but price moved {df['Close'].pct_change().loc[idx]*100:.2f}%",
                row_index=df.index.get_loc(idx),
                timestamp=idx.to_pydatetime() if hasattr(idx, 'to_pydatetime') else idx,
                column="Volume",
                value=float(df.loc[idx, 'Volume']),
                metadata={"price_change_pct": float(df['Close'].pct_change().loc[idx] * 100)}
            ))

        return issues

    def check_timestamp_issues(self, df: pd.DataFrame) -> List[QualityIssue]:
        """Check for timestamp issues (duplicates, non-monotonic, timezone issues)."""
        issues: List[QualityIssue] = []

        if len(df) < 2:
            return issues

        idx = pd.to_datetime(df.index)

        # Check for duplicates
        dupes = idx.duplicated(keep=False)
        if dupes.any():
            for idx_val in idx[dupes].unique():
                issues.append(QualityIssue(
                    check=QualityCheck.TIMESTAMP_ISSUE,
                    severity=QualitySeverity.WARNING,
                    message=f"Duplicate timestamp detected: {idx_val}",
                    timestamp=idx_val.to_pydatetime() if hasattr(idx_val, 'to_pydatetime') else idx_val,
                    metadata={"type": "duplicate_timestamp"}
                ))

        # Check for non-monotonic timestamps
        diffs = idx.to_series().diff()
        if (diffs < timedelta(0)).any():
            for idx_val in idx[diffs < timedelta(0)].index:
                issues.append(QualityIssue(
                    check=QualityCheck.TIMESTAMP_ISSUE,
                    severity=QualitySeverity.WARNING,
                    message=f"Non-monotonic timestamp: {idx[idx_val]} after {idx[idx_val-1]}",
                    row_index=idx_val,
                    timestamp=idx[idx_val].to_pydatetime() if hasattr(idx[idx_val], 'to_pydatetime') else idx[idx_val],
                    metadata={"type": "non_monotonic"}
                ))

        return issues

    def run_all_checks(self, df: pd.DataFrame, symbol: str, timeframe: str) -> QualityReport:
        """Run all quality checks and return aggregated report."""
        report = QualityReport(
            symbol=symbol,
            timeframe=timeframe,
            total_rows=len(df)
        )

        if df.empty:
            report.quality_score = 0.0
            report.add_issue(QualityIssue(
                check=QualityCheck.GAP_DETECTION,
                severity=QualitySeverity.CRITICAL,
                message="DataFrame is empty"
            ))
            return report

        # Run all checks
        all_issues: List[QualityIssue] = []

        all_issues.extend(self.check_gaps(df, timeframe))
        all_issues.extend(self.check_outliers(df))
        all_issues.extend(self.check_stale_quotes(df, symbol))
        all_issues.extend(self.check_ohlc_violations(df))
        all_issues.extend(self.check_volume_anomalies(df))
        all_issues.extend(self.check_timestamp_issues(df))

        # Add all issues to report
        for issue in all_issues:
            report.add_issue(issue)

        # Calculate quality score (0-1)
        critical_count = sum(1 for i in report.issues if i.severity == QualitySeverity.CRITICAL)
        warning_count = sum(1 for i in report.issues if i.severity == QualitySeverity.WARNING)
        info_count = sum(1 for i in report.issues if i.severity == QualitySeverity.INFO)

        # Score: start at 1.0, penalize for issues
        score = 1.0
        score -= critical_count * 0.2
        score -= warning_count * 0.05
        score -= info_count * 0.01
        report.quality_score = max(0.0, min(1.0, score))

        return report


# Convenience function for easy integration
def validate_data_quality(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    config: Optional[DataQualityConfig] = None
) -> QualityReport:
    """
    Convenience function to run all data quality checks on a DataFrame.
    """
    gate = DataQualityGate(config)
    return gate.run_all_checks(df, symbol, timeframe)


def should_skip_symbol(
    report: QualityReport,
    min_score: float = 0.6
) -> Tuple[bool, str]:
    """
    Convenience function to check whether a symbol should be skipped based on its QualityReport.
    """
    return report.should_skip(min_score=min_score)