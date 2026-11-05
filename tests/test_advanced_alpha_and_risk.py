import pytest
import pandas as pd
import numpy as np

from strategy.universe_screener import DynamicUniverseScreener
from strategy.breadth_filter import MarketBreadthFilter
from risk.correlation_manager import PortfolioCorrelationManager
from risk.risk_manager import RiskManager
from strategy.adaptive_strategy import TradeSignal

def _create_sample_df(n_bars=60, base_price=100.0, trend=0.1, vol_mult=1.0):
    prices = [base_price]
    for i in range(1, n_bars):
        prices.append(prices[-1] + trend + np.sin(i / 5.0) * 0.5)

    highs = [p + 0.8 for p in prices]
    lows = [p - 0.8 for p in prices]
    volumes = [100_000 * vol_mult] * n_bars

    df = pd.DataFrame({
        "Open": prices,
        "High": highs,
        "Low": lows,
        "Close": prices,
        "Volume": volumes
    })
    return df

def test_dynamic_universe_screener():
    screener = DynamicUniverseScreener(min_rvol=1.2, min_atr_pct=0.005, min_dollar_volume=100_000.0)

    # AAPL has RVOL expansion (last bar volume 3x)
    df_a = _create_sample_df(n_bars=50, base_price=100.0, trend=0.2, vol_mult=1.0)
    df_a.loc[df_a.index[-1], "Volume"] = 300_000

    # DEAD has normal / flat volume (rvol ~ 1.0 < 1.2)
    df_b = _create_sample_df(n_bars=50, base_price=50.0, trend=-0.1, vol_mult=1.0)

    frames = {"AAPL": df_a, "DEAD": df_b}
    results = screener.screen_candidates(frames)

    assert len(results) == 1
    assert results[0]["symbol"] == "AAPL"
    assert results[0]["rvol"] >= 1.2

def test_market_breadth_filter():
    breadth = MarketBreadthFilter(min_bullish_participation_pct=0.50)

    # 2 bullish frames (trending up), 1 bearish frame
    bull1 = _create_sample_df(n_bars=50, base_price=100.0, trend=0.3)
    bull2 = _create_sample_df(n_bars=50, base_price=200.0, trend=0.4)
    bear = _create_sample_df(n_bars=50, base_price=150.0, trend=-0.5)

    frames = {"SPY": bull1, "QQQ": bull2, "WEAK": bear}
    res = breadth.compute_market_breadth(frames)

    assert res["total_symbols"] == 3
    assert res["bullish_count"] >= 2
    assert res["healthy_breadth"] is True

def test_portfolio_correlation_manager():
    corr_mgr = PortfolioCorrelationManager(max_allowed_correlation=0.80)

    # Identical / perfectly correlated series
    s1 = pd.Series([10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 14.0, 14.5, 15.0, 15.5, 16.0, 16.5, 17.0, 17.5])
    s2 = pd.Series([20.0, 21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0, 28.0, 29.0, 30.0, 31.0, 32.0, 33.0, 34.0, 35.0])
    # Uncorrelated / oscillating series
    s3 = pd.Series([10.0, 9.0, 11.0, 8.5, 10.5, 9.2, 11.2, 8.7, 10.7, 9.1, 11.1, 8.9, 10.9, 9.3, 11.3, 8.8])

    corr = corr_mgr.calculate_pairwise_correlation(s1, s2)
    assert corr > 0.95

    # Should reject s2 because s1 is already in portfolio
    allowed, reason = corr_mgr.validate_candidate_correlation("MSFT", s2, {"AAPL": s1})
    assert allowed is False
    assert "Excessive portfolio correlation" in reason

    # Should allow s3
    allowed_uncorr, _ = corr_mgr.validate_candidate_correlation("UNREL", s3, {"AAPL": s1})
    assert allowed_uncorr is True

def test_risk_manager_correlation_rejection():
    corr_mgr = PortfolioCorrelationManager(max_allowed_correlation=0.80)
    rm = RiskManager(correlation_manager=corr_mgr)

    s1 = _create_sample_df(n_bars=30, base_price=100.0, trend=0.5)
    s2 = _create_sample_df(n_bars=30, base_price=200.0, trend=1.0) # Highly correlated with s1

    sig = TradeSignal(
        action="BUY",
        symbol="MSFT",
        entry_price=200.0,
        stop_loss_price=195.0,
        take_profit_price=210.0,
        confidence=0.85,
        regime="TRENDING_BULL",
        reason="Test",
        metadata={}
    )

    open_pos = {"AAPL": {"qty": 10, "entry_price": 100.0, "current_price": 105.0}}
    price_map = {"AAPL": s1, "MSFT": s2}

    res = rm.calculate_position_size(
        signal=sig,
        portfolio_equity=100000.0,
        cash_available=100000.0,
        current_open_positions_count=1,
        already_holding_symbol=False,
        open_positions=open_pos,
        price_history_map=price_map
    )

    assert res.allowed is False
    assert "Excessive portfolio correlation" in res.rejection_reason
