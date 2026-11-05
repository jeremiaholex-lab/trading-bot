"""
Paper-Trading Realism Layer
===========================
Provides realistic slippage and latency models for the local simulator.

SlippageModel
-------------
Composite model with three components:
  1. Spread cost       — half-spread based on liquidity tier (large/mid/small cap)
  2. Market impact     — Kyle-lambda style: sqrt(size/adv) × volatility × impact_factor
  3. Volatility regime — ATR-proportional widening in fast markets

LatencyModel
------------
Simulates realistic order-processing delays:
  - Submission latency: Gaussian(mean, σ) truncated at 0
  - Fill latency: Exponential(scale) for market orders
  - Market-hours multiplier: ×1.5 near open (09:30–09:45) and close (15:45–16:00 ET)
  - Optionally blocks (time.sleep) or returns ms value for logging-only mode

Both models are configured via Config flags and are only active in simulator mode.
They have no effect on live or Alpaca Paper Cloud orders.
"""

from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, time as dtime
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Liquidity tiers — approximate half-spreads for US equities by ADV bucket
# ---------------------------------------------------------------------------
#   Large-cap (ADV > $100M)  → ~1 bp half-spread
#   Mid-cap   (ADV $10M–$100M) → ~4 bp half-spread
#   Small-cap (ADV < $10M)   → ~12 bp half-spread
_LIQUIDITY_TIERS: list[tuple[float, float]] = [
    (100_000_000.0, 0.0001),   # large-cap: 1 bp
    (10_000_000.0,  0.0004),   # mid-cap:   4 bp
    (0.0,           0.0012),   # small-cap: 12 bp
]

# Well-known large-cap symbols — used when ADV data is unavailable
_KNOWN_LARGE_CAP = frozenset([
    "SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "GOOG", "META", "TSLA", "BRK.B", "JPM", "V", "UNH", "XOM", "JNJ",
    "AMD", "INTC", "SH", "PSQ", "SGOV",
])


def _half_spread(adv_usd: Optional[float], symbol: str) -> float:
    """Return estimated half-spread as a fraction of price."""
    if symbol.upper() in _KNOWN_LARGE_CAP:
        return 0.0001  # 1 bp
    if adv_usd is None:
        return 0.0004  # default to mid-cap when unknown
    for threshold, spread in _LIQUIDITY_TIERS:
        if adv_usd >= threshold:
            return spread
    return 0.0012


# ---------------------------------------------------------------------------
# SlippageResult
# ---------------------------------------------------------------------------

@dataclass
class SlippageResult:
    """Detailed breakdown of slippage components."""
    raw_price: float
    fill_price: float
    spread_cost: float      # fraction of price
    impact_cost: float      # fraction of price
    vol_cost: float         # fraction of price
    total_bps: float        # total slippage in basis points
    side: str               # 'buy' or 'sell'

    def log_line(self) -> str:
        return (
            f"fill={self.fill_price:.4f} raw={self.raw_price:.4f} "
            f"slippage={self.total_bps:.2f}bps "
            f"[spread={self.spread_cost*1e4:.1f}bp "
            f"impact={self.impact_cost*1e4:.1f}bp "
            f"vol={self.vol_cost*1e4:.1f}bp]"
        )


# ---------------------------------------------------------------------------
# SlippageModel
# ---------------------------------------------------------------------------

class SlippageModel:
    """
    Composite slippage model for the local paper simulator.

    Parameters
    ----------
    impact_factor : float
        Scaling constant for market-impact term (default 0.1). Increase for
        more aggressive impact on larger orders.
    min_bps : float
        Floor slippage in basis points — no fill is *better* than mid.
    max_bps : float
        Cap slippage in basis points — prevents unrealistic fills.
    seed : int | None
        Random seed for reproducible tests. None for live randomness.
    """

    def __init__(
        self,
        impact_factor: float = 0.1,
        min_bps: float = 0.5,
        max_bps: float = 150.0,
        seed: Optional[int] = None,
    ) -> None:
        self.impact_factor = impact_factor
        self.min_bps = min_bps
        self.max_bps = max_bps
        self._rng = random.Random(seed)

    def compute(
        self,
        price: float,
        qty: int,
        side: str,
        *,
        adv_usd: Optional[float] = None,
        atr: Optional[float] = None,
        symbol: str = "",
    ) -> SlippageResult:
        """
        Compute fill price with slippage for one order.

        Parameters
        ----------
        price   : float  — mid-market price
        qty     : int    — shares to trade
        side    : str    — 'buy' or 'sell'
        adv_usd : float  — average daily value traded in USD (optional)
        atr     : float  — average true range as fraction of price (optional)
        symbol  : str    — ticker, used for liquidity tier lookup

        Returns
        -------
        SlippageResult with fill_price already adjusted.
        """
        if price <= 0 or qty <= 0:
            return SlippageResult(
                raw_price=price, fill_price=price,
                spread_cost=0.0, impact_cost=0.0, vol_cost=0.0,
                total_bps=0.0, side=side,
            )

        # 1. Spread cost: always pay half-spread (buys at ask, sells at bid)
        spread = _half_spread(adv_usd, symbol)
        # Add slight randomness ±20% of spread to simulate tick variation
        spread *= 1.0 + self._rng.uniform(-0.2, 0.2)

        # 2. Market impact: Kyle-lambda approximation
        #    impact = impact_factor × sqrt(notional / adv) × (atr or default_vol)
        notional = price * qty
        default_vol = 0.015  # 1.5% daily vol if ATR unknown
        vol = atr if atr is not None else default_vol
        if adv_usd and adv_usd > 0:
            impact = self.impact_factor * math.sqrt(notional / adv_usd) * vol
        else:
            # No ADV data: use a conservative fixed impact
            impact = self.impact_factor * 0.01 * vol

        # 3. Volatility regime cost: extra half-spread widening in fast markets
        vol_regime = max(0.0, (vol - 0.01)) * 2.0  # zero below 1% daily vol

        # Total slippage fraction
        total_frac = spread + impact + vol_regime

        # Clamp to [min_bps, max_bps]
        min_frac = self.min_bps / 10_000.0
        max_frac = self.max_bps / 10_000.0
        total_frac = max(min_frac, min(max_frac, total_frac))

        # Direction: buys always fill above mid, sells below mid
        direction = 1.0 if side.lower() == "buy" else -1.0
        fill_price = round(price * (1.0 + direction * total_frac), 4)

        return SlippageResult(
            raw_price=price,
            fill_price=fill_price,
            spread_cost=spread,
            impact_cost=impact,
            vol_cost=vol_regime,
            total_bps=total_frac * 10_000.0,
            side=side,
        )


# ---------------------------------------------------------------------------
# LatencyResult
# ---------------------------------------------------------------------------

@dataclass
class LatencyResult:
    """Timing breakdown for a simulated order."""
    submission_ms: float
    fill_ms: float
    total_ms: float
    near_open: bool
    near_close: bool

    def log_line(self) -> str:
        tag = " [OPEN-RUSH]" if self.near_open else (" [CLOSE-RUSH]" if self.near_close else "")
        return f"latency={self.total_ms:.0f}ms (submit={self.submission_ms:.0f}ms fill={self.fill_ms:.0f}ms){tag}"


# ---------------------------------------------------------------------------
# LatencyModel
# ---------------------------------------------------------------------------

_ET_OPEN  = dtime(9, 30)
_ET_OPEN_END = dtime(9, 45)
_ET_CLOSE_START = dtime(15, 45)
_ET_CLOSE = dtime(16, 0)


def _market_hours_multiplier(now_et: Optional[dtime] = None) -> tuple[float, bool, bool]:
    """Return (multiplier, near_open, near_close) for the given ET time."""
    if now_et is None:
        return 1.0, False, False
    near_open = _ET_OPEN <= now_et < _ET_OPEN_END
    near_close = _ET_CLOSE_START <= now_et < _ET_CLOSE
    multiplier = 1.5 if (near_open or near_close) else 1.0
    return multiplier, near_open, near_close


class LatencyModel:
    """
    Simulates realistic order-processing latency for the local simulator.

    Submission latency: Gaussian(mean_ms, sigma_ms), truncated at 0.
    Fill latency: Exponential(scale=fill_mean_ms) — fast but variable.
    Market-hours: ×1.5 during opening and closing rush windows.

    Parameters
    ----------
    mean_ms      : float — mean submission latency in milliseconds (default 50)
    sigma_ms     : float — std-dev of submission latency (default 20)
    fill_mean_ms : float — mean fill latency for market orders (default 30)
    block        : bool  — if True, actually sleep; if False, just return ms values
    seed         : int | None — random seed for tests
    """

    def __init__(
        self,
        mean_ms: float = 50.0,
        sigma_ms: float = 20.0,
        fill_mean_ms: float = 30.0,
        block: bool = True,
        seed: Optional[int] = None,
    ) -> None:
        self.mean_ms = mean_ms
        self.sigma_ms = sigma_ms
        self.fill_mean_ms = fill_mean_ms
        self.block = block
        self._rng = random.Random(seed)

    def simulate(self, now_utc: Optional[datetime] = None) -> LatencyResult:
        """
        Simulate submission + fill latency for one order.

        Parameters
        ----------
        now_utc : datetime | None — current UTC time (uses system clock if None)

        Returns
        -------
        LatencyResult; also sleeps if self.block is True.
        """
        now_utc = now_utc or datetime.now(timezone.utc)
        from datetime import timedelta
        et_offset = timedelta(hours=-5)
        now_et_dt = now_utc + et_offset
        now_et = now_et_dt.time()

        multiplier, near_open, near_close = _market_hours_multiplier(now_et)

        # Submission latency: truncated Gaussian
        submit_ms = max(0.0, self._rng.gauss(self.mean_ms, self.sigma_ms)) * multiplier

        # Fill latency: exponential
        fill_ms = self._rng.expovariate(1.0 / max(1.0, self.fill_mean_ms)) * multiplier

        total_ms = submit_ms + fill_ms

        if self.block and total_ms > 0:
            time.sleep(total_ms / 1000.0)

        return LatencyResult(
            submission_ms=submit_ms,
            fill_ms=fill_ms,
            total_ms=total_ms,
            near_open=near_open,
            near_close=near_close,
        )


# ---------------------------------------------------------------------------
# FillEngine — combines SlippageModel + LatencyModel for one-call use
# ---------------------------------------------------------------------------

@dataclass
class FillResult:
    """Combined result from slippage + latency simulation."""
    slippage: SlippageResult
    latency: LatencyResult
    fill_price: float
    total_latency_ms: float

    def log_line(self) -> str:
        return f"{self.slippage.log_line()} | {self.latency.log_line()}"


class FillEngine:
    """
    Single entry-point that applies slippage and latency for a simulated fill.
    """

    def __init__(self, slippage: SlippageModel, latency: LatencyModel) -> None:
        self.slippage = slippage
        self.latency = latency

    @classmethod
    def from_config(cls, block_latency: bool = True) -> "FillEngine":
        """Construct FillEngine from Config flags."""
        from config import Config

        slip_enabled = getattr(Config, "SIM_SLIPPAGE_ENABLED", True)
        lat_enabled  = getattr(Config, "SIM_LATENCY_ENABLED", True)
        mean_ms      = getattr(Config, "SIM_LATENCY_MEAN_MS", 50.0)
        sigma_ms     = getattr(Config, "SIM_LATENCY_SIGMA_MS", 20.0)
        fill_ms      = getattr(Config, "SIM_LATENCY_FILL_MS", 30.0)
        impact       = getattr(Config, "SIM_IMPACT_FACTOR", 0.1)
        min_bps      = getattr(Config, "SIM_MIN_SLIPPAGE_BPS", 0.5)
        max_bps      = getattr(Config, "SIM_MAX_SLIPPAGE_BPS", 150.0)

        slippage_model = SlippageModel(
            impact_factor=impact,
            min_bps=min_bps if slip_enabled else 0.0,
            max_bps=max_bps if slip_enabled else 0.0,
        ) if slip_enabled else _NoSlippage()

        latency_model = LatencyModel(
            mean_ms=mean_ms,
            sigma_ms=sigma_ms,
            fill_mean_ms=fill_ms,
            block=block_latency,
        ) if lat_enabled else _NoLatency()

        return cls(slippage_model, latency_model)

    def fill(
        self,
        price: float,
        qty: int,
        side: str,
        *,
        adv_usd: Optional[float] = None,
        atr: Optional[float] = None,
        symbol: str = "",
        now_utc: Optional[datetime] = None,
    ) -> FillResult:
        """Apply slippage + latency and return FillResult."""
        slip = self.slippage.compute(price, qty, side, adv_usd=adv_usd, atr=atr, symbol=symbol)
        lat  = self.latency.simulate(now_utc=now_utc)
        return FillResult(
            slippage=slip,
            latency=lat,
            fill_price=slip.fill_price,
            total_latency_ms=lat.total_ms,
        )


class _NoSlippage(SlippageModel):
    def compute(self, price, qty, side, *, adv_usd=None, atr=None, symbol=""):
        return SlippageResult(
            raw_price=price, fill_price=price,
            spread_cost=0.0, impact_cost=0.0, vol_cost=0.0,
            total_bps=0.0, side=side,
        )


class _NoLatency(LatencyModel):
    def simulate(self, now_utc=None):
        return LatencyResult(submission_ms=0.0, fill_ms=0.0, total_ms=0.0,
                             near_open=False, near_close=False)
