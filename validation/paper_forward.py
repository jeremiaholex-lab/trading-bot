"""Prospective paper-forward comparison runbook (founder board condition 3).

The board voted FUND IF only if a prospective paper-forward comparison against a
named, simpler baseline — with realistic execution friction — beats it on a
risk-adjusted basis after costs. This module is the comparator and stop-criteria
evaluator for that run.

Design constraints
------------------
- **Pure and hermetic**: no network, no orders, no broker calls. The strategy
  and baseline equity curves are passed in, so the comparison is unit-testable
  without touching the market (a board safety invariant).
- **Metrics in agreement with the backtester**: Sharpe is computed exactly the
  way the optimizer computes it (`StrategyBacktester._annualised_sharpe`, daily
  resample, clipped to +/- MAX_PLAUSIBLE_SHARPE) so the validation and the
  walk-forward results mean the same thing. The architecture rule is that
  anything measuring the strategy must mirror the backtest.
- **Fail closed**: if the personal validation parameters are incomplete, the
  stop evaluator refuses to run rather than guessing the owner's risk tolerance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
import json
from pathlib import Path
from typing import List, Optional, Sequence, Union

import pandas as pd

from optimizer.backtester import StrategyBacktester
from validation.personal_validation import PersonalValidationParams


class StopStatus(str, Enum):
    """What the stop-criteria check concludes."""
    HOLD = "hold"   # evidence period not yet met; keep running
    PASS = "pass"   # evidence met and criteria satisfied
    STOP = "stop"   # a stop criterion fired


@dataclass
class PaperForwardResult:
    """Comparison of the strategy equity curve against the baseline curve."""
    strategy_return_pct: float
    baseline_return_pct: float
    alpha_pct: float              # strategy return minus baseline return
    strategy_sharpe: float
    baseline_sharpe: float
    strategy_max_drawdown_pct: float
    baseline_max_drawdown_pct: float
    beats_baseline: bool          # positive alpha AND Sharpe not worse
    total_snapshots: int


@dataclass
class StopEvaluation:
    """Outcome of checking the personal stop criteria against a result."""
    status: StopStatus
    reasons: List[str] = field(default_factory=list)


def _total_return_pct(curve: Sequence[float]) -> float:
    if len(curve) < 2 or curve[0] == 0:
        return 0.0
    return (curve[-1] - curve[0]) / curve[0] * 100.0


def _max_drawdown_pct(curve: Sequence[float]) -> float:
    if not curve:
        return 0.0
    peak = curve[0]
    max_dd = 0.0
    for value in curve:
        if value > peak:
            peak = value
        if peak > 0:
            dd = (peak - value) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd * 100.0


def _sharpe(curve: Sequence[float], index: Optional[pd.DatetimeIndex]) -> float:
    # Daily snapshots annualised with sqrt(252); a DatetimeIndex resamples to
    # daily closes first. Mirrors the backtester so the numbers agree.
    idx = index if index is not None else pd.RangeIndex(len(curve))
    return StrategyBacktester._annualised_sharpe(list(curve), idx)


def compare_equity_curves(
    strategy_curve: Sequence[float],
    baseline_curve: Sequence[float],
    index: Optional[Sequence[Union[str, datetime, date]]] = None,
) -> PaperForwardResult:
    """Compare the strategy equity curve against the baseline curve.

    `index`, when given, is the timestamp of each snapshot (used to resample to
    daily closes for the Sharpe ratio). When omitted, snapshots are assumed to
    be one per day.

    `beats_baseline` is True only when the strategy has *positive alpha* (returns
    more than the baseline) AND a *Sharpe at least as good* — so a higher-return
    / higher-drawdown outcome is not mistaken for success. This is the board's
    risk-adjusted hurdle.
    """
    strategy_curve = list(strategy_curve)
    baseline_curve = list(baseline_curve)

    idx: Optional[pd.DatetimeIndex] = None
    if index is not None:
        try:
            idx = pd.DatetimeIndex(pd.to_datetime(list(index)))
        except Exception:
            idx = None

    s_ret = _total_return_pct(strategy_curve)
    b_ret = _total_return_pct(baseline_curve)
    alpha = s_ret - b_ret
    s_sharpe = _sharpe(strategy_curve, idx)
    b_sharpe = _sharpe(baseline_curve, idx)
    s_dd = _max_drawdown_pct(strategy_curve)
    b_dd = _max_drawdown_pct(baseline_curve)

    beats = alpha > 0.0 and s_sharpe >= b_sharpe

    return PaperForwardResult(
        strategy_return_pct=round(s_ret, 2),
        baseline_return_pct=round(b_ret, 2),
        alpha_pct=round(alpha, 2),
        strategy_sharpe=round(s_sharpe, 2),
        baseline_sharpe=round(b_sharpe, 2),
        strategy_max_drawdown_pct=round(s_dd, 2),
        baseline_max_drawdown_pct=round(b_dd, 2),
        beats_baseline=beats,
        total_snapshots=len(strategy_curve),
    )


class StopCriteriaEvaluator:
    """Applies the owner's personal stop criteria to a comparison.

    Fail-closed: constructed with incomplete personal parameters and it raises,
    rather than guessing the owner's capital, drawdown tolerance, or time budget.
    """

    def __init__(self, params: PersonalValidationParams):
        self.params = params
        missing = params.validate()
        if missing:
            raise ValueError(
                "Personal validation parameters incomplete; refusing to guess "
                f"(missing: {', '.join(missing)})"
            )

    def evaluate(
        self,
        result: PaperForwardResult,
        *,
        observed_weeks: float,
        observed_drawdown_pct: Optional[float] = None,
        time_spent_hours_per_week: float = 0.0,
    ) -> StopEvaluation:
        """Check the stop criteria against a completed comparison.

        - If the evidence period is not yet met -> HOLD (too early to judge).
        - If any criterion fires -> STOP, with the reasons.
        - Otherwise -> PASS.
        """
        reasons: List[str] = []

        if observed_weeks < self.params.minimum_evidence_weeks:
            return StopEvaluation(
                status=StopStatus.HOLD,
                reasons=[
                    f"insufficient evidence: {observed_weeks:.1f} weeks observed, "
                    f"{self.params.minimum_evidence_weeks} required"
                ],
            )

        # Evidence period met -- now the criteria apply.
        if not result.beats_baseline:
            reasons.append(
                f"paper-forward did not beat '{self.params.named_baseline}' "
                f"(alpha {result.alpha_pct:.2f}% required >0)"
            )

        dd = observed_drawdown_pct
        if dd is None:
            dd = result.strategy_max_drawdown_pct
        limit_pct = self.params.max_acceptable_drawdown_pct * 100.0
        if dd > limit_pct:
            reasons.append(
                f"drawdown {dd:.1f}% exceeds limit {limit_pct:.0f}%"
            )

        budget = self.params.monitoring_time_budget_hours_per_week
        if budget and time_spent_hours_per_week > budget:
            reasons.append(
                f"time spent {time_spent_hours_per_week:.1f}h/wk exceeds "
                f"budget {budget:.1f}h/wk"
            )

        if reasons:
            return StopEvaluation(status=StopStatus.STOP, reasons=reasons)
        return StopEvaluation(status=StopStatus.PASS)


class PaperForwardJournal:
    """Records prospective daily snapshots for a paper-forward run.

    The actual forward accumulation (running the bot in paper mode and reading
    its equity, plus the baseline's closing price) is the caller's job; this
    journal stores the snapshots so the run can span weeks and be evaluated at
    the end without losing history across restarts.

    A snapshot is (timestamp, strategy_equity, baseline_equity). Persisted to
    JSON when a path is given; otherwise kept in memory (used by tests).
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else None
        self._snapshots: List[dict] = []
        self.meta: dict = {}
        if self.path is not None and self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self._snapshots = list(data.get("snapshots", []))
                self.meta = dict(data.get("meta", {}))
            except (json.JSONDecodeError, OSError):
                self._snapshots = []
                self.meta = {}

    def record_snapshot(
        self,
        timestamp: Union[str, datetime, date],
        strategy_equity: float,
        baseline_equity: float,
    ) -> None:
        self._snapshots.append({
            "timestamp": pd.Timestamp(timestamp).isoformat(),
            "strategy_equity": float(strategy_equity),
            "baseline_equity": float(baseline_equity),
        })
        self._save()

    def _save(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"meta": self.meta, "snapshots": self._snapshots}, indent=2)
            )

    @property
    def snapshots(self) -> List[dict]:
        return list(self._snapshots)

    def to_result(self) -> Optional[PaperForwardResult]:
        if len(self._snapshots) < 2:
            return None
        strategy_curve = [s["strategy_equity"] for s in self._snapshots]
        baseline_curve = [s["baseline_equity"] for s in self._snapshots]
        index = [s["timestamp"] for s in self._snapshots]
        return compare_equity_curves(strategy_curve, baseline_curve, index)
