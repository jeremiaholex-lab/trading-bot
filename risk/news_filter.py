import logging
import datetime as dt
import gzip
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

try:
    # defusedxml neutralises XXE and billion-laughs attacks in untrusted XML
    # (Yahoo RSS and the SEC EDGAR feed). Fall back to the stdlib parser only
    # if it is genuinely not installed, so the filter still runs.
    from defusedxml import ElementTree as ET
    _HAVE_DEFUSEDXML = True
except ImportError:  # pragma: no cover - environment without defusedxml
    import xml.etree.ElementTree as ET
    _HAVE_DEFUSEDXML = False

logger = logging.getLogger(__name__)

if not _HAVE_DEFUSEDXML:  # pragma: no cover
    logger.warning(
        "defusedxml not installed; parsing untrusted news XML with the stdlib "
        "parser (XXE/billion-laughs risk). Run 'pip install defusedxml'."
    )

# --------------------------------------------------------------------------
# Sources (free, no key). Ported from the trader-news skill (day-trader-skill,
# MIT) so the bot can see the same event risk a human monitor would.
# --------------------------------------------------------------------------
YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=US&lang=en-US"
SEC_FEED = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=%s&type=8-K"
    "&dateb=&owner=include&count=%d&output=atom"
)
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
BROWSER_UA = "Mozilla/5.0 (compatible; trading-bot/1.0)"
PLACEHOLDER_UA = "you@example.com"

# 8-K items that signal material event risk. A company files one of these and
# the next bar can gap in a direction the 15-minute setup cannot know about.
IMPORTANT_8K_ITEMS = {
    "1.03": "bankruptcy",
    "1.05": "cybersecurity incident",
    "2.02": "earnings results",
    "2.04": "debt acceleration",
    "2.06": "impairment",
    "3.01": "delisting notice",
    "4.02": "past financials no longer reliable",
    "5.01": "change in control",
    "5.02": "director or officer change",
}

# Headline patterns that flag a move the setup cannot price in. Each entry is
# (regex, label); a match is treated as event risk by default.
RISK_HEADLINES = [
    (r"\brecall\b|\bbankrupt|\bdefault\b|\bdelist", "default/recall risk"),
    (r"\blawsuit\b|\bsued\b|\bprobe\b|\binvestigation\b|\bSEC charges\b|\bantitrust\b",
     "legal risk"),
    (r"\bhalt(?:s|ed)?\b|\btrading (?:pause|suspen)", "trading halt"),
    (r"\bhack(?:ed|ing)?\b|\bbreach(?:ed)?\b|\boutage\b|"
     r"\bcyber(?:attack|incident|securit|breach)", "cyber/outage"),
    (r"\bdowngrade[sd]?\b", "downgrade"),
]


def _local(tag: str) -> str:
    """ElementTree tag without the namespace prefix."""
    return tag.rsplit("}", 1)[-1]


def _text(el, name: str) -> str:
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def _parse_time(value: Optional[str]) -> Optional[dt.datetime]:
    """RFC 822 (RSS) or ISO 8601 (Atom) -> aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


class NewsFilter:
    """Guards against event/gap risk from breaking news and SEC filings.

    Fetches Yahoo Finance headlines and SEC EDGAR 8-K filings for a symbol and
    reports whether there is material event risk within a lookback window.

    The filter is deliberately permissive on failure: if the feed is
    unreachable it logs a warning and lets the trade through, because a
    network blip must not silently halt all trading on a false positive.
    (The bot's price signal is the primary gate; news is a secondary guard.)
    """

    def __init__(self, cache_ttl_hours: float = 6.0, sec_user_agent: str = "",
                 pause: float = 0.2):
        self.cache_ttl_hours = cache_ttl_hours
        self.sec_user_agent = sec_user_agent.strip()
        self.pause = pause
        self._last_fetch = 0.0
        self._ciks: Dict[str, str] = {}
        # Cache: {symbol: (Optional[List[dict]], float_cached)}
        self._cache: Dict[str, Tuple[Optional[List[dict]], float]] = {}

    # ------------------------------------------------------------------ fetch

    def _get(self, url: str, sec: bool = False) -> bytes:
        if sec and not self.sec_user_agent:
            raise NewsFetchError("no SEC User-Agent set")
        gap = time.time() - self._last_fetch
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last_fetch = time.time()
        headers = {
            "User-Agent": self.sec_user_agent if sec else BROWSER_UA,
            "Accept-Encoding": "gzip",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsFetchError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise NewsFetchError("could not reach %s: %s" % (url, exc.reason))

    def _cik_map(self) -> Dict[str, str]:
        """ticker -> 10-digit CIK, from SEC's company_tickers.json (cached)."""
        if self._ciks:
            return self._ciks
        data = self._get(TICKERS_URL, sec=True).decode("utf-8")
        import json
        doc = json.loads(data)
        for v in doc.values():
            self._ciks.setdefault(
                v["ticker"].upper().replace("-", "."), str(v["cik_str"]).zfill(10)
            )
        return self._ciks

    # ---------------------------------------------------------------- parsing

    def _fetch_headlines(self, symbol: str) -> List[dict]:
        url = YAHOO_RSS % urllib.parse.quote(symbol)
        data = self._get(url)
        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            raise NewsFetchError("RSS for %s is not valid XML: %s" % (symbol, exc))
        out = []
        for it in root.iter("item"):
            title = _text(it, "title")
            link = _text(it, "link")
            guid = _text(it, "guid") or link
            out.append({
                "id": "rss:" + guid, "ticker": symbol, "title": title,
                "summary": _text(it, "description"), "link": link,
                "time": _parse_time(_text(it, "pubDate")),
            })
        return out

    def _fetch_8k(self, symbol: str) -> List[dict]:
        ciks = self._cik_map()
        cik = ciks.get(symbol)
        if not cik:
            return []
        data = self._get(SEC_FEED % (cik, 10), sec=True)
        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            raise NewsFetchError("SEC feed for %s is not valid XML: %s" % (symbol, exc))
        out = []
        for entry in root.iter():
            if _local(entry.tag) != "entry":
                continue
            form = _text(entry, "filing-type")
            acc = _text(entry, "accession-number")
            items = re.findall(r"\d+\.\d+", _text(entry, "items-desc"))
            labels = [IMPORTANT_8K_ITEMS.get(i, "item " + i)
                      for i in items if i != "9.01"]
            title = "SEC %s filed: %s" % (form, ", ".join(labels)) if labels \
                else "SEC %s filed" % form
            out.append({
                "id": "sec:" + acc, "ticker": symbol, "title": title,
                "form": form, "items": items,
                "important": bool(set(items) & set(IMPORTANT_8K_ITEMS)),
                "time": _parse_time(_text(entry, "updated")),
            })
        return out

    # ---------------------------------------------------------------- the check

    def _collect(self, symbol: str) -> List[dict]:
        """Headlines + 8-K for one symbol. Raises on total failure."""
        items = self._fetch_headlines(symbol)
        if self.sec_user_agent:
            try:
                items += self._fetch_8k(symbol)
            except NewsFetchError as exc:
                logger.debug("SEC 8-K for %s unavailable (%s); headlines only", symbol, exc)
        return items

    def is_blocked(
        self,
        symbol: str,
        hours: Optional[float] = None,
        important_only: Optional[bool] = None,
        now: Optional[dt.datetime] = None,
    ) -> Tuple[bool, str]:
        """(blocked, reason) if there is material event risk for `symbol`.

        `hours` is the lookback window; `important_only` True blocks only on
        important 8-K items and risk-keyword headlines, False blocks on any
        headline or filing in the window. On a fetch failure it logs and
        returns (False, "") so a transient outage never halts trading.
        """
        now = now or dt.datetime.now(dt.timezone.utc)

        cached = self._cache.get(symbol)
        if cached and (now.timestamp() - cached[1]) < self.cache_ttl_hours * 3600:
            return self._evaluate(cached[0], now, hours, important_only)

        try:
            items = self._collect(symbol)
        except NewsFetchError as exc:
            logger.warning("News filter could not fetch %s (%s); allowing trade", symbol, exc)
            self._cache[symbol] = ([], now.timestamp())
            return False, ""

        self._cache[symbol] = (items, now.timestamp())
        return self._evaluate(items, now, hours, important_only)

    @staticmethod
    def _mentions_symbol(symbol: str, text: str) -> bool:
        """True if `text` names the ticker (e.g. 'NVDA' or '$NVDA').

        A Yahoo headline feed carries market-wide news too, so an item should
        only gate a symbol it is actually about. SEC filings are exempt (see
        _evaluate) because a company's own 8-K is unambiguously about it.
        """
        if not symbol or not text:
            return False
        return bool(re.search(
            r"(?<![A-Za-z0-9])" + re.escape(symbol) + r"(?![A-Za-z0-9])",
            text, re.I,
        ))

    @staticmethod
    def _evaluate(items, now, hours, important_only) -> Tuple[bool, str]:
        hours = hours if hours is not None else 24.0
        important_only = True if important_only is None else important_only
        cutoff = now - dt.timedelta(hours=hours)
        ahead = now + dt.timedelta(minutes=10)  # tolerate clock skew on publish time

        for it in items:
            when = it.get("time")
            if when is not None and (when < cutoff or when > ahead):
                continue
            symbol = it.get("ticker", "")
            is_filing = bool(it.get("form"))

            if important_only:
                # A company's own 8-K is always about it: gate on important items.
                if it.get("important"):
                    return True, "Important SEC filing: %s" % it["title"]
                # Non-important filings do not gate; only risk headlines do.
                if is_filing:
                    continue
                hay = "%s %s" % (it["title"], it.get("summary") or "")
                # A headline gates only if it is about this symbol. Without
                # this, a market-wide headline (e.g. an exchange hack) in any
                # ticker's feed would halt every trade.
                if not NewsFilter._mentions_symbol(symbol, hay):
                    continue
                for rx, label in RISK_HEADLINES:
                    if re.search(rx, hay, re.I):
                        return True, "News risk (%s): %s" % (label, it["title"])
            else:
                return True, "Breaking item within %dh window: %s" % (
                    int(hours), it["title"])
        return False, ""


class NewsFetchError(Exception):
    """Raised when a news feed cannot be reached or parsed."""
