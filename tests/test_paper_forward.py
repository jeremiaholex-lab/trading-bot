"""Hermetic tests for the prospective paper-forward comparison runbook.

These test the comparator and stop-criteria evaluator in isolation: no network,
no orders, no broker. The safety invariant is that validation logic must never
touch the market.
"""
import json
from pathlib import Path

import pytest

from validation.paper_forward import (
    PaperForwardJournal,
    StopCriteriaEvaluator,
    StopStatus,
    compare_equity_curves,
    _max_drawdown_pct,
    _total_return_pct,
)
from validation.personal_validation import PersonalValidationParams


def make_params(**overrides):
    base = dict(
        personal_outcome="beat buy-and-hold SPY risk-adjusted",
        named_baseline="buy-and-hold SPY",
        max_capital_exposure_usd=25000.0,
        max_acceptable_drawdown_pct=0.10,
        monitoring_time_budget_hours_per_week=3.0,
        development_time_budget_hours_per_month=5.0,
        stop_criteria="stop if it fails",
        minimum_evidence_weeks=12,
    )
    base.update(overrides)
    return PersonalValidationParams(**base)


# --- Return / drawdown primitives ------------------------------------------

def test_total_return_pct():
    assert _total_return_pct([100.0, 110.0]) == pytest.approx(10.0)
    assert _total_return_pct([100.0, 90.0]) == pytest.approx(-10.0)
    assert _total_return_pct([]) == 0.0
    assert _total_return_pct([100.0]) == 0.0


def test_max_drawdown_pct():
    # Peak 120 -> trough 90 -> recovers to 130: drawdown 25%.
    assert _max_drawdown_pct([100.0, 120.0, 90.0, 130.0]) == pytest.approx(25.0)
    assert _max_drawdown_pct([100.0, 110.0]) == 0.0
    assert _max_drawdown_pct([]) == 0.0


# --- Comparator -------------------------------------------------------------

def test_compare_computes_alpha_and_return():
    strategy = [100.0, 105.0, 110.0]     # +10%
    baseline = [100.0, 101.0, 102.0]     # +2%
    res = compare_equity_curves(strategy, baseline)
    assert res.strategy_return_pct == pytest.approx(10.0)
    assert res.baseline_return_pct == pytest.approx(2.0)
    assert res.alpha_pct == pytest.approx(8.0)
    assert res.total_snapshots == 3


def test_beats_baseline_when_alpha_positive_and_risk_ok():
    strategy = [100.0, 104.0, 108.0, 112.0]   # steady up, high Sharpe
    baseline = [100.0, 100.5, 101.0, 101.5]   # flat, lower Sharpe
    res = compare_equity_curves(strategy, baseline)
    assert res.alpha_pct > 0
    assert res.beats_baseline is True


def test_not_beats_when_alpha_positive_but_sharpe_worse():
    # Strategy returns MORE (+15%) but with violent swings -> Sharpe ~4.3,
    # below the baseline's clipped 5.0. Positive alpha alone is not enough;
    # the risk-adjusted hurdle is what the board demands.
    strategy = [100.0, 140.0, 90.0, 140.0, 90.0, 115.0]
    baseline = [100.0, 101.0, 102.0, 103.0, 104.0, 110.0]
    res = compare_equity_curves(strategy, baseline)
    assert res.alpha_pct > 0
    assert res.strategy_sharpe < res.baseline_sharpe
    assert res.beats_baseline is False


def test_not_beats_when_alpha_negative():
    strategy = [100.0, 98.0, 96.0]   # -4%
    baseline = [100.0, 101.0, 102.0]  # +2%
    res = compare_equity_curves(strategy, baseline)
    assert res.alpha_pct < 0
    assert res.beats_baseline is False


def test_compare_handles_datetime_index():
    import pandas as pd
    index = pd.date_range("2026-01-01", periods=4, freq="D")
    strategy = [100.0, 104.0, 108.0, 112.0]
    baseline = [100.0, 100.5, 101.0, 101.5]
    res = compare_equity_curves(strategy, baseline, index)
    assert res.total_snapshots == 4
    assert res.beats_baseline is True


# --- Stop criteria evaluator ------------------------------------------------

def test_stop_evaluator_raises_on_incomplete_params():
    # Fail-closed: an unset drawdown tolerance must raise, not guess.
    incomplete = make_params(max_acceptable_drawdown_pct=None)
    with pytest.raises(ValueError, match="incomplete"):
        StopCriteriaEvaluator(incomplete)


def test_stop_holds_until_evidence_period_met():
    params = make_params(minimum_evidence_weeks=12)
    evaluator = StopCriteriaEvaluator(params)
    strategy = [100.0, 105.0, 110.0]
    baseline = [100.0, 100.5, 101.0]
    res = compare_equity_curves(strategy, baseline)
    # Only 8 weeks observed -> too early to judge, even though it currently beats.
    ev = evaluator.evaluate(res, observed_weeks=8.0)
    assert ev.status == StopStatus.HOLD
    assert ev.reasons


def test_stop_passes_when_criteria_satisfied():
    params = make_params(minimum_evidence_weeks=12)
    evaluator = StopCriteriaEvaluator(params)
    strategy = [100.0, 104.0, 108.0, 112.0]
    baseline = [100.0, 100.5, 101.0, 101.5]
    res = compare_equity_curves(strategy, baseline)
    ev = evaluator.evaluate(res, observed_weeks=13.0, time_spent_hours_per_week=1.0)
    assert ev.status == StopStatus.PASS
    assert ev.reasons == []


def test_stop_fires_when_baseline_not_beaten():
    params = make_params(minimum_evidence_weeks=12)
    evaluator = StopCriteriaEvaluator(params)
    strategy = [100.0, 98.0, 96.0]
    baseline = [100.0, 101.0, 102.0]
    res = compare_equity_curves(strategy, baseline)
    ev = evaluator.evaluate(res, observed_weeks=13.0)
    assert ev.status == StopStatus.STOP
    assert any("did not beat" in r for r in ev.reasons)


def test_stop_fires_when_drawdown_exceeds_limit():
    params = make_params(minimum_evidence_weeks=12, max_acceptable_drawdown_pct=0.05)
    evaluator = StopCriteriaEvaluator(params)
    strategy = [100.0, 120.0, 90.0, 130.0]   # 25% drawdown
    baseline = [100.0, 100.5, 101.0, 101.5]
    res = compare_equity_curves(strategy, baseline)
    ev = evaluator.evaluate(res, observed_weeks=13.0)
    assert ev.status == StopStatus.STOP
    assert any("drawdown" in r for r in ev.reasons)


# --- Journal ----------------------------------------------------------------

def test_journal_reloads_and_builds_result(tmp_path):
    path = tmp_path / "journal.json"
    journal = PaperForwardJournal(path)
    journal.record_snapshot("2026-01-01", 100.0, 100.0)
    journal.record_snapshot("2026-01-02", 105.0, 101.0)
    journal.record_snapshot("2026-01-03", 110.0, 102.0)

    # Persisted, so a fresh journal reads it back.
    reloaded = PaperForwardJournal(path)
    assert len(reloaded.snapshots) == 3
    res = reloaded.to_result()
    assert res is not None
    assert res.total_snapshots == 3
    assert res.alpha_pct == pytest.approx(8.0)  # +10% strategy vs +2% baseline

    # The raw file is valid JSON.
    data = json.loads(path.read_text())
    assert len(data["snapshots"]) == 3


def test_journal_needs_two_snapshots():
    journal = PaperForwardJournal()
    journal.record_snapshot("2026-01-01", 100.0, 100.0)
    assert journal.to_result() is None


# --- Runbook CLI ------------------------------------------------------------

class _FakeBroker:
    def __init__(self, equity=100000.0, mode="LOCAL_SIMULATOR"):
        self.equity = equity
        self.mode = mode

    def get_account_summary(self):
        return {"equity": self.equity, "mode": self.mode}


def test_record_snapshot_aligns_baseline_to_strategy_start(tmp_path):
    from validation.runbook_cli import record_paper_forward_snapshot
    path = str(tmp_path / "journal.json")
    broker = _FakeBroker(equity=100000.0, mode="LOCAL_SIMULATOR")

    # First snapshot: baseline aligned to the strategy's starting equity.
    s1 = record_paper_forward_snapshot(broker, baseline_price=500.0, journal_path=path)
    assert s1["snapshots"] == 1
    assert s1["status"] == "need_more_snapshots"

    # Second: strategy +5%, baseline also +5% (525/500) -> zero alpha.
    broker.equity = 105000.0
    s2 = record_paper_forward_snapshot(broker, baseline_price=525.0, journal_path=path)
    assert s2["snapshots"] == 2
    assert s2["result"].strategy_return_pct == pytest.approx(5.0)
    assert s2["result"].baseline_return_pct == pytest.approx(5.0)
    assert s2["result"].alpha_pct == pytest.approx(0.0)


def test_record_snapshot_refuses_live_broker(tmp_path):
    from validation.runbook_cli import record_paper_forward_snapshot
    broker = _FakeBroker(equity=100000.0, mode="LIVE")
    with pytest.raises(ValueError, match="LIVE"):
        record_paper_forward_snapshot(
            broker, baseline_price=500.0, journal_path=str(tmp_path / "j.json")
        )


def test_evaluate_run_need_more_snapshots(tmp_path):
    from validation.runbook_cli import evaluate_run
    summary = evaluate_run(journal_path=str(tmp_path / "empty.json"))
    assert summary["status"] == "need_more_snapshots"


def test_record_snapshot_fails_closed_on_incomplete_params(tmp_path, monkeypatch):
    import validation.runbook_cli as rc
    from validation.personal_validation import PersonalValidationParams
    incomplete = PersonalValidationParams(
        personal_outcome="beat baseline",
        named_baseline="buy-and-hold SPY",
    )
    monkeypatch.setattr(rc, "PERSONAL_VALIDATION", incomplete)
    with pytest.raises(ValueError, match="incomplete"):
        rc.record_paper_forward_snapshot(
            _FakeBroker(), baseline_price=500.0, journal_path=str(tmp_path / "j.json")
        )
