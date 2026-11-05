"""Personal validation parameters for the trading bot.

These are the "conditions to keep going" from the founder board review
(founder/board.md). They are personal context that only the owner can supply —
capital, risk tolerance, time budget, the simpler baseline to beat, and the
explicit stop criteria. The board concluded these inputs "are currently
unknown," and no downstream decision (paper-forward trial, live consideration)
should be made without them.

This module is a decision record, not runtime bot configuration. The paper
forward-comparison runbook reads `PERSONAL_VALIDATION` so the same parameters
drive the validation and the stop decision.

Usage:
    from validation.personal_validation import PERSONAL_VALIDATION
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PersonalValidationParams:
    """Owner-defined personal context for the validation decision.

    The board's conditions 1 and 2 (founder/board.md) require these to be
    stated *before* testing. Leave any as `None` and `validate()` will refuse
    to run — failing closed, not silently guessing.
    """

    # -- Condition 1: the personal outcome and the simpler baseline ----------
    # What the bot must deliver for you, and what you would otherwise do.
    # E.g. "beat buy-and-hold SPY on risk-adjusted return after costs" or
    # "save me X hours of manual screening per week with no worse drawdown."
    personal_outcome: str | None = None

    # The named, simpler alternative you are comparing against. The board
    # insists this be predeclared, not chosen after seeing results.
    # E.g. "buy-and-hold SPY" or "long-only QQQ".
    named_baseline: str | None = None

    # -- Condition 2: exposure, downside, and time budget --------------------
    # Maximum personal capital you are willing to expose to this strategy.
    max_capital_exposure_usd: float | None = None

    # Largest equity drawdown you will accept before stopping.
    # E.g. 0.10 for 10%.
    max_acceptable_drawdown_pct: float | None = None

    # Time you are willing to spend monitoring/operating the bot per week.
    monitoring_time_budget_hours_per_week: float | None = None

    # Time you are willing to spend developing/maintaining it per month.
    development_time_budget_hours_per_month: float | None = None

    # Explicit stop criteria — the conditions under which you simplify or stop.
    # E.g. "stop if paper-forward does not beat the baseline over 12 weeks, or
    # if realized drawdown exceeds the limit."
    stop_criteria: str | None = None

    # -- Conditions 3-6: the validation-run spec (technical defaults) --------
    # Minimum prospective evidence period before live trading is even
    # considered. The board says no live until this is met and the downside
    # is explicitly accepted.
    minimum_evidence_weeks: int = 12

    # Require validation on untouched periods and more than one market regime.
    require_untouched_periods: bool = True
    require_multiple_regimes: bool = True

    # Conservative execution friction for the paper-forward comparison.
    # Slippage in bps, and whether to include a borrow-cost assumption for
    # shorts (currently NOT modeled by the simulator — see README).
    assumed_slippage_bps: float = 5.0
    include_borrow_cost_assumption: bool = False

    # Free-form notes for the next board review.
    notes: str = field(default="")

    def validate(self) -> list[str]:
        """Return a list of missing personal parameters.

        Empty list means the personal context is complete. Any non-empty result
        must be surfaced (and treated as fatal for a validation run) rather
        than silently proceeding with guessed values.
        """
        missing: list[str] = []
        if not self.personal_outcome:
            missing.append("personal_outcome")
        if not self.named_baseline:
            missing.append("named_baseline")
        if self.max_capital_exposure_usd is None:
            missing.append("max_capital_exposure_usd")
        if self.max_acceptable_drawdown_pct is None:
            missing.append("max_acceptable_drawdown_pct")
        if self.monitoring_time_budget_hours_per_week is None:
            missing.append("monitoring_time_budget_hours_per_week")
        if self.development_time_budget_hours_per_month is None:
            missing.append("development_time_budget_hours_per_month")
        if not self.stop_criteria:
            missing.append("stop_criteria")
        return missing


# ---------------------------------------------------------------------------
# The owner's personal parameters. FILL THIS IN.
#
# REVIEW: These are SUGGESTED conservative starting values, chosen for a
# personal-use validation project with no proven edge (the founder board's
# framing). They are NOT the owner's confirmed risk tolerance — the board
# requires these to be your own numbers, stated and accepted before any live
# use. Adjust each to your actual capital, tolerance, and time budget.
# ---------------------------------------------------------------------------
PERSONAL_VALIDATION = PersonalValidationParams(
    personal_outcome=(
        "Beat buy-and-hold SPY on risk-adjusted return (Sharpe) after realistic "
        "costs, while keeping drawdown within the stated limit."
    ),
    named_baseline="buy-and-hold SPY",
    max_capital_exposure_usd=25000.0,
    max_acceptable_drawdown_pct=0.10,
    monitoring_time_budget_hours_per_week=3.0,
    development_time_budget_hours_per_month=5.0,
    stop_criteria=(
        "Stop if the prospective paper-forward comparison does not beat "
        "buy-and-hold SPY on risk-adjusted return after costs over the 12-week "
        "minimum evidence period, or if realized drawdown exceeds 10% of exposed "
        "capital, or if monitoring/development time exceeds the stated budgets."
    ),
    notes=(
        "Suggested conservative starting values; owner must review and confirm "
        "before live use."
    ),
)
