"""CLI helpers for the prospective paper-forward validation (board condition 3).

These are the thin, runnable wrappers around the pure comparator in
`paper_forward.py`. They are called from `bot.py`'s `--paper-forward` /
`--paper-forward-evaluate` commands so the owner can record a daily snapshot and
check the stop criteria without writing any code.

Fail-closed: if the personal validation parameters are incomplete, these raise
rather than guessing the owner's risk tolerance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from data.market_data import MarketDataProvider
from validation.paper_forward import (
    PaperForwardJournal,
    StopCriteriaEvaluator,
    StopStatus,
)
from validation.personal_validation import PERSONAL_VALIDATION


def observed_weeks(journal: PaperForwardJournal) -> float:
    """Whole weeks elapsed between the first and last snapshot."""
    snaps = journal.snapshots
    if len(snaps) < 2:
        return 0.0
    t0 = pd.Timestamp(snaps[0]["timestamp"])
    t1 = pd.Timestamp(snaps[-1]["timestamp"])
    return (t1 - t0).total_seconds() / (7.0 * 86400.0)


def _summary(journal: PaperForwardJournal) -> Dict[str, Any]:
    params = PERSONAL_VALIDATION
    missing = params.validate()
    if missing:
        raise ValueError(
            "Personal validation parameters incomplete; refusing to evaluate "
            f"(missing: {', '.join(missing)})"
        )

    result = journal.to_result()
    weeks = observed_weeks(journal)

    if result is None:
        return {
            "snapshots": len(journal.snapshots),
            "observed_weeks": round(weeks, 2),
            "result": None,
            "status": "need_more_snapshots",
            "reasons": ["need at least 2 daily snapshots to compare"],
        }

    evaluator = StopCriteriaEvaluator(params)
    stop = evaluator.evaluate(result, observed_weeks=weeks)
    return {
        "snapshots": len(journal.snapshots),
        "observed_weeks": round(weeks, 2),
        "result": result,
        "status": stop.status.value,
        "reasons": list(stop.reasons),
    }


def evaluate_run(journal_path: Optional[str] = None) -> Dict[str, Any]:
    """Evaluate the stop criteria against an existing journal (no recording)."""
    journal = PaperForwardJournal(Path(journal_path) if journal_path else None)
    return _summary(journal)


def record_paper_forward_snapshot(
    broker: Any,
    baseline_symbol: str = "SPY",
    baseline_price: Optional[float] = None,
    journal_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Record one daily snapshot and return the updated comparison.

    The baseline (buy-and-hold `baseline_symbol`) is aligned to the strategy's
    starting equity on the first snapshot, so both curves start from the same
    base and the risk-adjusted comparison is fair.
    """
    params = PERSONAL_VALIDATION
    missing = params.validate()
    if missing:
        raise ValueError(
            "Personal validation parameters incomplete; refusing to record "
            f"(missing: {', '.join(missing)})"
        )

    # Fail-closed: a paper-forward validation must not be recorded from a live
    # broker. The board's condition 6 keeps live trading off until the
    # predeclared evidence threshold is met.
    acc = broker.get_account_summary()
    mode = str(acc.get("mode", "")).upper()
    if "LIVE" in mode:
        raise ValueError(
            "Refusing to record paper-forward against a LIVE broker "
            "(board condition 6: no live until the evidence threshold is met)"
        )
    strategy_equity = float(acc.get("equity", 0.0))

    price = baseline_price
    if price is None:
        price = MarketDataProvider().get_latest_price(baseline_symbol)
    if price is None:
        raise ValueError(f"Could not determine a baseline price for {baseline_symbol}")

    journal = PaperForwardJournal(Path(journal_path) if journal_path else None)

    if not journal.meta.get("baseline_start_price"):
        # First snapshot: lock the baseline to the strategy's start.
        journal.meta["baseline_symbol"] = baseline_symbol
        journal.meta["baseline_start_price"] = float(price)
        journal.meta["baseline_base_equity"] = strategy_equity
        baseline_equity = strategy_equity
    else:
        start_price = journal.meta["baseline_start_price"]
        base = journal.meta["baseline_base_equity"]
        baseline_equity = base * (float(price) / start_price) if start_price else strategy_equity

    journal.record_snapshot(datetime.now(timezone.utc), strategy_equity, baseline_equity)
    return _summary(journal)
