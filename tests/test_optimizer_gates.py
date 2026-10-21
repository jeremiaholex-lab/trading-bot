"""Tests for the empirically calibrated optimizer gates.

The gate values were chosen from a rolling-origin calibration over 311
(symbol, origin) pairs across seven liquid symbols, using a third untouched
forward slice to measure what actually happened after each adoption.

Two facts from that calibration drive these tests:

  * A candidate with a high out-of-sample fitness is NOT reliably better
    than the defaults. The signal that mattered was the SIZE of the
    improvement (candidate fitness minus baseline fitness).
  * The bot can only trade a handful of times a month, so an absolute
    trade-count gate is nearly unreachable in a short window -- and the
    gates must be expressed in a way that a daily-cadence bot can meet.
"""
import pytest

from config import Config
from optimizer.backtester import BacktestResult
from optimizer.self_improver import SelfImprover
from strategy.adaptive_strategy import StrategyParameters


def _result(trades=30, wins=18, pnl=5.0, pf=1.4, dd=8.0, sharpe=0.9, fitness=3.0):
    """Builds a BacktestResult with consistent trade counts."""
    return BacktestResult(
        total_trades=trades,
        winning_trades=wins,
        losing_trades=trades - wins,
        win_rate=round(wins / trades * 100, 2),
        total_pnl_pct=pnl,
        profit_factor=pf,
        max_drawdown_pct=dd,
        sharpe_ratio=sharpe,
        fitness_score=fitness,
    )


def test_default_fitness_gate_is_calibrated():
    """The fitness floor must be the calibrated value."""
    assert Config.MIN_FITNESS_TO_ADOPT == 0.0


def test_default_trade_gate_is_reachable_for_a_daily_cadence_bot():
    """The trade-count gate must be satisfiable with a realistically sized sample.

    The strategy produces roughly one trade per month per symbol. A gate
    demanding many trades inside a short window rejects every candidate
    forever, which means the bot never adapts at all -- the optimiser
    becomes decoration.
    """
    assert Config.MIN_TRADES_TO_ADOPT <= 3, (
        f"MIN_TRADES_TO_ADOPT={Config.MIN_TRADES_TO_ADOPT} is too high for a "
        f"strategy that trades a few times a month; every candidate would be "
        f"rejected and the bot would never re-tune."
    )


def test_optimizer_adopts_when_the_candidate_clears_both_gates(monkeypatch, tmp_path):
    """A clearly better candidate must be adopted."""
    from optimizer.backtester import StrategyBacktester

    def rigged(df, params, symbol="TICKER", initial_capital=10000.0, **kwargs):
        if params.to_dict() == StrategyParameters().to_dict():
            # The baseline: profitable but weak.
            return _result(30, 18, 5.0, 1.4, 8.0, 0.9, 3.0)
        # The candidate: clearly better.
        return _result(25, 16, 10.0, 2.2, 4.0, 1.4, 9.0)

    monkeypatch.setattr(StrategyBacktester, "run_backtest", staticmethod(rigged))

    improver = SelfImprover(state_file=tmp_path / "state.json")
    result = improver.optimize_symbol("SPY", _bars(200))

    assert result["improved"] is True


def test_optimizer_keeps_parameters_when_the_candidate_is_not_better(monkeypatch, tmp_path):
    """A candidate that does not beat the baseline must not be adopted."""
    from optimizer.backtester import StrategyBacktester

    def rigged(df, params, symbol="TICKER", initial_capital=10000.0, **kwargs):
        if params.to_dict() == StrategyParameters().to_dict():
            return _result(30, 18, 5.0, 1.4, 8.0, 0.9, 3.0)
        return _result(25, 13, 2.0, 1.1, 11.0, 0.4, 1.0)

    monkeypatch.setattr(StrategyBacktester, "run_backtest", staticmethod(rigged))

    improver = SelfImprover(state_file=tmp_path / "state.json")
    result = improver.optimize_symbol("SPY", _bars(200))

    assert result["improved"] is False


def test_optimizer_rejects_a_high_fitness_candidate_that_is_worse_than_the_baseline(
    monkeypatch, tmp_path
):
    """A high score alone is not enough -- it must also beat the baseline.

    The calibration showed that candidates with a high absolute fitness were
    no more likely to go on to work than candidates with a low one. What
    predicted a good outcome was the SIZE OF THE IMPROVEMENT over the
    baseline, not the absolute level.
    """
    from optimizer.backtester import StrategyBacktester

    def rigged(df, params, symbol="TICKER", initial_capital=10000.0, **kwargs):
        if params.to_dict() == StrategyParameters().to_dict():
            # A very strong baseline.
            return _result(30, 22, 20.0, 3.0, 3.0, 2.2, 12.0)
        # A candidate with a high absolute score but WORSE than the baseline.
        return _result(25, 15, 6.0, 1.6, 6.0, 1.1, 5.0)

    monkeypatch.setattr(StrategyBacktester, "run_backtest", staticmethod(rigged))

    improver = SelfImprover(state_file=tmp_path / "state.json")
    result = improver.optimize_symbol("SPY", _bars(200))

    assert result["improved"] is False, (
        "adopted a candidate that was worse than the parameters already in use"
    )


def test_optimizer_requires_a_minimum_improvement_margin(monkeypatch, tmp_path):
    """A hairline win is not evidence; the margin must be material."""
    from optimizer.backtester import StrategyBacktester

    def rigged(df, params, symbol="TICKER", initial_capital=10000.0, **kwargs):
        if params.to_dict() == StrategyParameters().to_dict():
            return _result(30, 18, 5.0, 1.4, 8.0, 0.9, 3.0)
        # Barely better: +0.05 fitness.
        return _result(25, 16, 5.5, 1.45, 8.0, 0.95, 3.05)

    monkeypatch.setattr(StrategyBacktester, "run_backtest", staticmethod(rigged))

    improver = SelfImprover(state_file=tmp_path / "state.json")
    result = improver.optimize_symbol("SPY", _bars(200))

    assert result["improved"] is False, (
        "adopted a candidate on a hairline improvement over the baseline"
    )


def _bars(n=200):
    import numpy as np
    import pandas as pd

    close = 100 + np.linspace(0, 20, n)
    return pd.DataFrame(
        {
            "Open": close, "High": close + 0.5, "Low": close - 0.5,
            "Close": close, "Volume": np.full(n, 10000),
        },
        index=pd.date_range("2026-01-01", periods=n, freq="15min"),
    )
