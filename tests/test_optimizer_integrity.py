"""Regression tests for optimizer integrity.

The optimizer is the "intelligence" of the bot: it decides which strategy
parameters to trade with. If it selects on the wrong data, adopts parameters
that lose money, or ranks candidates by an unstable metric, then every other
part of the system is working toward the wrong target.

The rigged backtester below lets each test describe exactly two candidates:
one that looks good on the training slice and one that only looks good on the
held-out slice. A correct optimizer picks the first; a leaky one picks the
second.
"""
import numpy as np
import pandas as pd
import pytest

from config import Config
from optimizer.backtester import BacktestResult, StrategyBacktester
from optimizer.self_improver import SelfImprover
from strategy.adaptive_strategy import StrategyParameters

DEFAULT_PARAMS = StrategyParameters()
SPLIT_AT = 0.70


def _result(trades=20, wins=12, pnl=8.0, pf=2.0, dd=4.0, sharpe=1.2, fitness=6.0):
    return BacktestResult(
        total_trades=trades,
        winning_trades=wins,
        losing_trades=trades - wins,
        win_rate=round(wins / trades * 100, 2) if trades else 0.0,
        total_pnl_pct=pnl,
        profit_factor=pf,
        max_drawdown_pct=dd,
        sharpe_ratio=sharpe,
        fitness_score=fitness,
    )


def _is_default(params):
    """True when the backtester is evaluating the *baseline* parameters."""
    return params.to_dict() == DEFAULT_PARAMS.to_dict()


def _split_lengths(n):
    split = int(n * SPLIT_AT)
    return split, n - split


def _is_train_slice(df, n):
    return len(df) == _split_lengths(n)[0]


def _is_test_slice(df, n):
    return len(df) == _split_lengths(n)[1]


@pytest.fixture
def sample_data():
    """Deterministic 200-bar uptrend."""
    n = 200
    close = 100 + np.linspace(0, 20, n)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + 0.5,
            "Low": close - 0.5,
            "Close": close,
            "Volume": np.full(n, 10000),
        },
        index=pd.date_range("2026-01-01", periods=n, freq="15min"),
    )


# ----------------------------------------------------------------------
# Bug: the walk-forward "held out" test set was used to SELECT the best
# candidate, so it was not held out at all. Every parameter the bot
# adopted was selected on the data it was then scored against.
# ----------------------------------------------------------------------
def test_optimizer_does_not_adopt_a_test_only_winner(monkeypatch, sample_data, tmp_path):
    """A candidate that is only good on the held-out slice must not be adopted.

    Two candidates: "train_good" scores well in-sample and poorly out of
    sample; "test_good" scores poorly in-sample and brilliantly out of
    sample. Ranking on the training slice must select train_good.
    """
    n = len(sample_data)

    def rigged(df, params, symbol="TICKER", initial_capital=10000.0, **kwargs):
        if _is_train_slice(df, n):
            if params.ema_fast == 13:
                # Looks poor in-sample.
                return _result(trades=12, wins=6, pnl=1.0, pf=1.1, dd=5.0, sharpe=0.4, fitness=2.0)
            # Looks good in-sample.
            return _result(trades=12, wins=6, pnl=4.0, pf=1.6, dd=4.0, sharpe=0.9, fitness=5.0)
        # Held-out slice.
        if _is_default(params):
            return _result(trades=20, wins=12, pnl=8.0, pf=2.0, dd=4.0, sharpe=1.2, fitness=6.0)
        if params.ema_fast == 13:
            # Spectacular out of sample -- but never selected in a correct run.
            return _result(trades=20, wins=18, pnl=45.0, pf=9.0, dd=1.0, sharpe=4.0, fitness=50.0)
        return _result(trades=20, wins=9, pnl=-4.0, pf=0.7, dd=9.0, sharpe=-0.4, fitness=-2.0)

    monkeypatch.setattr(StrategyBacktester, "run_backtest", staticmethod(rigged))

    improver = SelfImprover(state_file=tmp_path / "state.json")
    result = improver.optimize_symbol("SPY", sample_data)

    assert result["improved"] is False, (
        "adopted parameters that were only good on the held-out slice; "
        "the test set must never influence selection."
    )


def test_optimizer_evaluates_the_test_slice_only_once(monkeypatch, sample_data, tmp_path):
    """The held-out slice must be scored on the winner only, not per candidate.

    The number of held-out evaluations must not grow with the size of the
    parameter grid -- that is exactly what turns a holdout into a training set.
    """
    n = len(sample_data)
    calls = {"train": 0, "test": 0}

    def counting(df, params, symbol="TICKER", initial_capital=10000.0, **kwargs):
        if _is_train_slice(df, n):
            calls["train"] += 1
            # Always healthy in-sample so the candidate loop is never
            # short-circuited before the held-out slice is reached.
            return _result(trades=12, wins=7, pnl=3.0, pf=1.3, dd=5.0, sharpe=0.8, fitness=1.5)
        if _is_test_slice(df, n):
            calls["test"] += 1
            return _result(trades=12, wins=7, pnl=3.0, pf=1.3, dd=5.0, sharpe=0.8, fitness=1.5)
        return _result()

    monkeypatch.setattr(StrategyBacktester, "run_backtest", staticmethod(counting))

    improver = SelfImprover(state_file=tmp_path / "state.json")
    improver.optimize_symbol("SPY", sample_data)

    assert calls["train"] > 1, "expected multiple training evaluations"
    assert calls["test"] <= 2, (
        f"the held-out test slice was evaluated {calls['test']} times. It must be "
        f"scored on the single candidate selected by training (plus the baseline "
        f"for comparison) -- never once per candidate."
    )


# ----------------------------------------------------------------------
# Bug: `improved` was a purely RELATIVE comparison against the baseline.
# The backtester returns fitness = -999.0 when there are no trades, so a
# baseline that never traded was beaten by any candidate that traded at
# all. The bot shipped SPY with fitness -0.64 marked "improved": true.
# ----------------------------------------------------------------------
def test_optimizer_will_not_adopt_below_the_absolute_fitness_floor(monkeypatch, sample_data, tmp_path):
    """A candidate below MIN_FITNESS_TO_ADOPT must never be adopted.

    The candidate clears the in-sample gates (positive fitness, enough
    trades) so it is genuinely considered -- but on the held-out slice it
    is a losing strategy, and the baseline is worse still, so a purely
    relative comparison would call it an improvement.
    """
    n = len(sample_data)

    def rigged(df, params, symbol="TICKER", initial_capital=10000.0, **kwargs):
        if _is_default(params):
            return _result(trades=12, wins=2, pnl=-30.0, pf=0.2, dd=60.0, sharpe=-3.0, fitness=-40.0)
        if _is_train_slice(df, n):
            # Clears the in-sample gate.
            return _result(trades=12, wins=7, pnl=4.0, pf=1.6, dd=4.0, sharpe=0.9, fitness=5.0)
        # Out of sample it is a losing strategy -- but better than baseline.
        return _result(trades=12, wins=5, pnl=-6.0, pf=0.6, dd=25.0, sharpe=-1.2, fitness=-6.0)

    monkeypatch.setattr(StrategyBacktester, "run_backtest", staticmethod(rigged))

    improver = SelfImprover(state_file=tmp_path / "state.json")
    result = improver.optimize_symbol("SPY", sample_data)

    assert result["improved"] is False, (
        "adopted a losing strategy: the absolute fitness floor was not enforced."
    )


def test_optimizer_will_not_adopt_on_a_thin_out_of_sample_sample(monkeypatch, sample_data, tmp_path):
    """A candidate with too few out-of-sample trades is not evidence."""
    n = len(sample_data)

    def rigged(df, params, symbol="TICKER", initial_capital=10000.0, **kwargs):
        if _is_default(params):
            return _result(trades=30, wins=20, pnl=12.0, pf=2.0, dd=5.0, sharpe=1.5, fitness=8.0)
        if _is_train_slice(df, n):
            # Strong in-sample: passes the training gates.
            return _result(trades=25, wins=16, pnl=10.0, pf=2.2, dd=4.0, sharpe=1.4, fitness=9.0)
        # Only 2 out-of-sample trades -- not enough to trust.
        return _result(trades=2, wins=2, pnl=30.0, pf=5.0, dd=0.0, sharpe=3.0, fitness=40.0)

    monkeypatch.setattr(StrategyBacktester, "run_backtest", staticmethod(rigged))

    improver = SelfImprover(state_file=tmp_path / "state.json")
    result = improver.optimize_symbol("SPY", sample_data)

    assert result["improved"] is False, (
        "adopted a strategy supported by only a couple of out-of-sample trades."
    )


def test_optimizer_adopts_a_candidate_that_clears_both_gates(monkeypatch, sample_data, tmp_path):
    """The gates must not be so strict that nothing is ever adopted."""
    n = len(sample_data)

    def rigged(df, params, symbol="TICKER", initial_capital=10000.0, **kwargs):
        if _is_default(params):
            return _result(trades=30, wins=18, pnl=5.0, pf=1.4, dd=8.0, sharpe=0.9, fitness=3.0)
        # Solid on both slices: 25 trades, positive fitness.
        return _result(trades=25, wins=16, pnl=10.0, pf=2.2, dd=4.0, sharpe=1.4, fitness=9.0)

    monkeypatch.setattr(StrategyBacktester, "run_backtest", staticmethod(rigged))

    improver = SelfImprover(state_file=tmp_path / "state.json")
    result = improver.optimize_symbol("SPY", sample_data)

    assert result["improved"] is True
    assert result["best_fitness"] >= Config.MIN_FITNESS_TO_ADOPT


# ----------------------------------------------------------------------
# Bug: the parameter search used an unseeded random.sample(), so every
# calibration run explored a different subset and the bot's parameters
# drifted non-reproducibly.
# ----------------------------------------------------------------------
def test_optimizer_is_deterministic_across_runs(sample_data, tmp_path):
    """Two runs on identical data must select identical parameters."""
    results = []
    for i in range(2):
        improver = SelfImprover(state_file=tmp_path / f"state_{i}.json")
        results.append(improver.optimize_symbol("SPY", sample_data))

    assert results[0]["best_params"] == results[1]["best_params"], (
        "the parameter search is not reproducible; the bot's parameters drift "
        "randomly on every calibration run."
    )


# ----------------------------------------------------------------------
# Bug: the Sharpe ratio was computed from a handful of TRADE returns and
# then annualised with sqrt(252) -- assuming 252 trades per year. With 7
# trades that implies 36 years of trading, and the ratio explodes
# (measured: Sharpe 1096 from 7 trades, fitness 442). It carried 40% of
# the fitness weight, so the optimizer was mostly ranking by this artefact.
# ----------------------------------------------------------------------
def _sawtooth_data(n=200, seed=0):
    """A sawtooth uptrend that produces real entries for the strategy."""
    rng = np.random.default_rng(seed)
    px, p = [], 100.0
    for i in range(n):
        phase = i % 23
        if phase < 20:
            p *= 1.006
        else:
            p *= 0.97
        px.append(p)
    close = np.array(px) + rng.normal(0, 0.05, n)
    return pd.DataFrame(
        {
            "Open": close, "High": close + 0.4, "Low": close - 0.4,
            "Close": close, "Volume": rng.integers(5000, 20000, n),
        },
        index=pd.date_range("2026-01-01", periods=n, freq="15min"),
    )


def test_sharpe_ratio_is_bounded_on_a_small_sample():
    """A handful of trades must not yield a Sharpe in the hundreds."""
    df = _sawtooth_data(200)
    result = StrategyBacktester.run_backtest(df, StrategyParameters(), symbol="SPY")

    assert result.total_trades > 0, "fixture should produce trades"
    assert abs(result.sharpe_ratio) < 10.0, (
        f"Sharpe ratio of {result.sharpe_ratio} on {result.total_trades} trades is "
        f"an artefact of annualising a tiny sample of TRADE returns with sqrt(252)."
    )


def test_fitness_score_is_not_dominated_by_the_sharpe_term():
    """Fitness must stay in a sane range so the optimizer ranks on real edge."""
    df = _sawtooth_data(200)
    result = StrategyBacktester.run_backtest(df, StrategyParameters(), symbol="SPY")

    assert abs(result.fitness_score) < 50.0, (
        f"fitness score of {result.fitness_score} on {result.total_trades} trades is "
        f"dominated by the inflated Sharpe term, which carries 40% of the weight."
    )


def test_backtester_reports_zero_sharpe_without_trades():
    """No trades must not produce a NaN or absurd Sharpe."""
    n = 120
    df = pd.DataFrame(
        {
            "Open": np.full(n, 100.0), "High": np.full(n, 100.0),
            "Low": np.full(n, 100.0), "Close": np.full(n, 100.0),
            "Volume": np.full(n, 10000),
        },
        index=pd.date_range("2026-01-01", periods=n, freq="15min"),
    )
    result = StrategyBacktester.run_backtest(df, StrategyParameters(), symbol="SPY")

    assert result.total_trades == 0
    assert result.sharpe_ratio == 0.0
    assert result.fitness_score < 0, "a strategy that never trades must score badly"
