import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple, Optional, Any
from zoneinfo import ZoneInfo
from config import Config

logger = logging.getLogger(__name__)

class MacroCalendarFilter:
    """
    Guards against high-impact macroeconomic event risk (FOMC rate decisions,
    CPI prints, Non-Farm Payrolls - NFP).
    Technical signals and bracket stops fail during extreme release volatility.

    Enforces a blackout window (e.g. 30 minutes before and after scheduled release).
    """

    def __init__(
        self,
        blackout_minutes_before: int = 30,
        blackout_minutes_after: int = 30,
        custom_events: Optional[List[Dict[str, Any]]] = None
    ):
        self.blackout_before = timedelta(minutes=blackout_minutes_before)
        self.blackout_after = timedelta(minutes=blackout_minutes_after)
        self.custom_events = custom_events or []

    def get_known_macro_events(self, year: int) -> List[Dict[str, Any]]:
        """
        Returns schedule of major US economic events.
        Times are typically:
          - CPI: 08:30 AM ET (before market open)
          - NFP: First Friday of the month at 08:30 AM ET
          - FOMC Rate Decision & Press Conference: 14:00 & 14:30 ET
        """
        events = list(self.custom_events)

        # Standard FOMC Wednesday meeting windows (sample anchor schedule)
        # 8 meetings per year typically in Jan, Mar, May, Jun, Jul, Sep, Nov, Dec
        fomc_months_days = [(1, 29), (3, 19), (5, 7), (6, 18), (7, 30), (9, 17), (11, 5), (12, 10)]
        for month, day in fomc_months_days:
            try:
                # FOMC statement is 14:00 ET, press conference at 14:30 ET
                et_tz = ZoneInfo("America/New_York")
                dt_et = datetime(year, month, day, 14, 0, tzinfo=et_tz)
                events.append({
                    "name": "FOMC Rate Decision & Press Conference",
                    "time_utc": dt_et.astimezone(timezone.utc),
                    "impact": "HIGH"
                })
            except Exception:
                continue

        # Standard CPI release dates (mid-month ~10th-14th at 8:30 AM ET)
        for month in range(1, 13):
            try:
                et_tz = ZoneInfo("America/New_York")
                cpi_dt = datetime(year, month, 12, 8, 30, tzinfo=et_tz)
                events.append({
                    "name": "US Consumer Price Index (CPI)",
                    "time_utc": cpi_dt.astimezone(timezone.utc),
                    "impact": "HIGH"
                })
            except Exception:
                continue

        return events

    def is_in_macro_blackout(
        self,
        current_time_utc: Optional[datetime] = None
    ) -> Tuple[bool, str]:
        """
        Checks whether the given time falls within any high-impact economic blackout window.
        """
        now_utc = current_time_utc or datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)

        events = self.get_known_macro_events(now_utc.year)

        for ev in events:
            ev_time = ev["time_utc"]
            if ev_time.tzinfo is None:
                ev_time = ev_time.replace(tzinfo=timezone.utc)

            window_start = ev_time - self.blackout_before
            window_end = ev_time + self.blackout_after

            if window_start <= now_utc <= window_end:
                mins_diff = int((ev_time - now_utc).total_seconds() / 60.0)
                if mins_diff >= 0:
                    status_desc = f"in {mins_diff}m"
                else:
                    status_desc = f"{abs(mins_diff)}m ago"
                return True, f"Macro Event Blackout: {ev['name']} ({status_desc})"

        return False, ""
