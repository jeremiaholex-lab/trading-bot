import logging
from typing import Tuple, Optional
from config import Config

logger = logging.getLogger(__name__)

class BidAskSpreadFilter:
    """
    Guards against excessive market maker friction and illiquidity slippage.
    Checks relative spread: (Ask - Bid) / Mid.

    If relative spread exceeds MAX_SPREAD_PCT (e.g. > 0.15%), new orders
    are blocked to avoid immediate transaction cost bleed.
    """

    def __init__(self, max_spread_pct: Optional[float] = None):
        self.max_spread_pct = max_spread_pct if max_spread_pct is not None else getattr(Config, "MAX_SPREAD_PCT", 0.0015)

    def validate_spread(
        self,
        symbol: str,
        bid_price: float,
        ask_price: float
    ) -> Tuple[bool, str]:
        """
        Validates bid and ask prices against the spread threshold.
        """
        if bid_price <= 0 or ask_price <= 0:
            return True, "No bid/ask quote available; bypassing spread filter"

        if ask_price < bid_price:
            return False, f"Invalid inverted quote on {symbol}: Bid ${bid_price:.2f} > Ask ${ask_price:.2f}"

        mid = (bid_price + ask_price) / 2.0
        spread = ask_price - bid_price
        rel_spread = spread / mid if mid > 0 else 0.0

        if rel_spread > self.max_spread_pct:
            return False, (
                f"Relative spread on {symbol} is {rel_spread * 100:.3f}% (${spread:.2f} / ${mid:.2f}), "
                f"exceeding maximum permitted {self.max_spread_pct * 100:.2f}%. Gating entry to prevent slippage bleed."
            )

        return True, f"Spread healthy ({rel_spread * 100:.3f}%)"
