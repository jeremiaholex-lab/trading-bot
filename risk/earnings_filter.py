import logging
from datetime import datetime, date, timezone, timedelta
from typing import Optional, Dict, Tuple
import yfinance as yf

logger = logging.getLogger(__name__)

class EarningsFilter:
    """
    Guards against binary event risk by identifying upcoming and recent
    corporate earnings announcements.
    """

    def __init__(self, cache_ttl_hours: float = 12.0):
        self.cache_ttl_hours = cache_ttl_hours
        # Cache format: {symbol: (Optional[date], datetime_cached)}
        self._cache: Dict[str, Tuple[Optional[date], datetime]] = {}

    def get_next_earnings_date(self, symbol: str) -> Optional[date]:
        """
        Retrieves the scheduled earnings date for a given symbol.
        Uses in-memory cache to minimize external network requests.
        """
        now = datetime.now(timezone.utc)
        if symbol in self._cache:
            cached_date, cached_time = self._cache[symbol]
            if (now - cached_time).total_seconds() < self.cache_ttl_hours * 3600.0:
                return cached_date

        earnings_date = self._fetch_earnings_date(symbol)
        self._cache[symbol] = (earnings_date, now)
        return earnings_date

    def _fetch_earnings_date(self, symbol: str) -> Optional[date]:
        """Fetches earnings calendar from yfinance safely."""
        try:
            ticker = yf.Ticker(symbol)
            calendar = getattr(ticker, "calendar", None)
            if not calendar or not isinstance(calendar, dict):
                return None

            earnings_dates = calendar.get("Earnings Date")
            if not earnings_dates:
                return None

            if isinstance(earnings_dates, (list, tuple)):
                raw_date = earnings_dates[0]
            else:
                raw_date = earnings_dates

            if hasattr(raw_date, "date"):
                return raw_date.date()
            elif isinstance(raw_date, date):
                return raw_date
            elif isinstance(raw_date, str):
                return datetime.fromisoformat(raw_date[:10]).date()
        except Exception as e:
            logger.debug(f"Could not retrieve earnings date for {symbol}: {e}")
            return None

        return None

    def is_in_blackout(
        self,
        symbol: str,
        blackout_days: int = 2,
        current_date: Optional[date] = None
    ) -> Tuple[bool, str]:
        """
        Returns (True, reason) if trading the symbol is restricted due to proximity
        to an earnings release.
        """
        earnings_dt = self.get_next_earnings_date(symbol)
        if earnings_dt is None:
            return False, ""

        today = current_date or datetime.now(timezone.utc).date()
        days_until = (earnings_dt - today).days

        # Pre-earnings blackout (e.g. 0 to blackout_days ahead)
        if 0 <= days_until <= blackout_days:
            if days_until == 0:
                return True, f"Earnings release scheduled TODAY ({earnings_dt})"
            return True, f"Earnings release in {days_until} day(s) on {earnings_dt} (within {blackout_days}d blackout)"

        # Post-earnings immediate volatility window (1 day after)
        if -1 <= days_until < 0:
            return True, f"Earnings reported on {earnings_dt}; post-earnings volatility blackout"

        return False, ""
