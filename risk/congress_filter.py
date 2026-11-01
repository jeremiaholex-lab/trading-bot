import logging
import datetime as dt
import io
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
import zipfile
from typing import Dict, List, Optional

try:
    # defusedxml neutralises XXE and billion-laughs attacks in the untrusted
    # House Clerk index XML. Fall back to the stdlib parser only if missing.
    from defusedxml import ElementTree as ET
    _HAVE_DEFUSEDXML = True
except ImportError:  # pragma: no cover
    import xml.etree.ElementTree as ET
    _HAVE_DEFUSEDXML = False

logger = logging.getLogger(__name__)

if not _HAVE_DEFUSEDXML:  # pragma: no cover
    logger.warning(
        "defusedxml not installed; parsing untrusted congressional XML with "
        "the stdlib parser. Run 'pip install defusedxml'."
    )

INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/%dFD.zip"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/%d/%s.pdf"
UA = "trading-bot/1.0 (+https://github.com/jbolex10-lab/trading-bot)"

# A PTR is a public STOCK Act disclosure. It can arrive up to 45 days after
# the trade, and amounts are ranges, not prices. It is a signal, not a fact.
LATE_DAYS = 45
TXN_TYPES = {"P": "purchase", "S": "sale", "S (partial)": "partial sale", "E": "exchange"}

TXN = re.compile(
    r"^\s*(?:(?P<owner>SP|JT|DC)\s+)?(?P<asset>.*?)\s+(?P<type>S \(partial\)|P|S|E)\s+"
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<notified>\d{2}/\d{2}/\d{4})\s+(?P<amount>\S.*?)\s*$"
)
PAGE_HEADER = re.compile(r"^\s*(ID\s+Owner\s+Asset|Type\s+Date\b|Type$|Date$|Gains >$|\$200\?$)", re.I)
END = re.compile(r"^\s*(\* For the complete|I CERTIFY|Digitally Signed|Filing ID #)", re.I)


def _iso(mdy: Optional[str]) -> Optional[str]:
    """'8/21/2026' -> '2026-08-21'; two-digit years -> 20xx."""
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$", mdy or "")
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


class CongressFilter:
    """Advisory watcher for congressional stock-trade disclosures.

    Reads the House Clerk's public Periodic Transaction Reports (PTRs) and
    reports any that involve a ticker the bot watches. This is an ALPHA
    signal, not a risk gate: a member buying a stock does not make it unsafe
    to own, so it is never used to block a trade. It is off by default and
    needs `pdftotext` (poppler) to map a filing to its tickers.
    """

    def __init__(self, cache_ttl_hours: float = 6.0, pdf_dir: Optional[str] = None):
        self.cache_ttl_hours = cache_ttl_hours
        self.pdf_dir = pdf_dir
        self._cache: Dict[str, List[dict]] = {}
        self._cached_at: Dict[str, float] = {}

    @staticmethod
    def _download(url: str, timeout: int = 60) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise CongressError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise CongressError("could not reach %s: %s" % (url, exc.reason))

    @staticmethod
    def _parse_index(data: bytes, year: int) -> List[dict]:
        if data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = [n for n in z.namelist() if n.lower().endswith(".xml")]
                if not names:
                    return []
                data = z.read(names[0])
        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            raise CongressError("index is not valid XML: %s" % exc)
        out = []
        for m in root.iter("Member"):
            f = {c.tag: (c.text or "").strip() for c in m}
            doc = f.get("DocID", "")
            kind = f.get("FilingType", "")
            y = int(f.get("Year") or year)
            name = " ".join(x for x in (f.get("Prefix"), f.get("First"), f.get("Last"),
                                        f.get("Suffix")) if x)
            out.append({
                "doc_id": doc, "type": kind, "name": name,
                "last": f.get("Last", ""), "first": f.get("First", ""),
                "district": f.get("StateDst", ""), "year": y,
                "filed": _iso(f.get("FilingDate", "")),
                "url": PTR_URL % (y, doc) if kind == "P" else "",
            })
        return out

    def _load_index(self, year: int) -> List[dict]:
        key = str(year)
        now = dt.datetime.now().timestamp()
        if key in self._cached_at and (now - self._cached_at[key]) < self.cache_ttl_hours * 3600:
            return self._cache[key]
        rows = self._parse_index(self._download(INDEX_URL % year), year)
        self._cache[key] = rows
        self._cached_at[key] = now
        return rows

    @staticmethod
    def _pdf_to_text(pdf_path: str) -> Optional[str]:
        exe = shutil.which("pdftotext")
        if not exe:
            return None
        try:
            out = subprocess.run([exe, "-layout", pdf_path, "-"],
                                 capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else None

    @staticmethod
    def _extract_tickers(text: str) -> List[str]:
        tickers, seen = [], set()
        for line in text.splitlines():
            m = TXN.match(line.rstrip())
            if not m:
                continue
            asset = m.group("asset")
            for t in re.findall(r"\(([A-Z][A-Z0-9.\-/]{0,9})\)", asset):
                t = t.upper()
                if t not in seen:
                    seen.add(t)
                    tickers.append(t)
        return tickers

    def _read_ptrs(self, rows: List[dict], year: int) -> List[dict]:
        """Download and parse recent PTR PDFs; drop those without pdftotext."""
        if not shutil.which("pdftotext"):
            logger.debug("Congress watch: pdftotext not installed; skipping PTR reads")
            return []
        folder = self.pdf_dir or "logs/ptr-pdfs"
        os.makedirs(folder, exist_ok=True)
        reports = []
        for r in rows:
            path = os.path.join(folder, "%s.pdf" % r["doc_id"])
            try:
                if not os.path.isfile(path):
                    with open(path, "wb") as fh:
                        fh.write(self._download(PTR_URL % (year, r["doc_id"])))
                text = self._pdf_to_text(path)
            except (CongressError, OSError):
                continue
            if not text:
                continue
            reports.append({
                "doc_id": r["doc_id"], "member": r["name"], "district": r["district"],
                "filed": r["filed"], "url": r["url"],
                "tickers": self._extract_tickers(text),
            })
        return reports

    def recent_trades(
        self,
        symbols: Optional[List[str]] = None,
        since_days: int = LATE_DAYS,
        read_pdfs: bool = True,
        now: Optional[dt.date] = None,
    ) -> List[dict]:
        """Recent PTRs whose tickers intersect `symbols` (advisory, never a block).

        Returns a list of {member, district, doc_id, filed, tickers, url}. On
        an index fetch failure it logs a warning and returns [] so the bot
        never treats a congressional-data outage as a reason to halt.
        """
        symbols = {s.upper() for s in (symbols or [])}
        today = now or dt.date.today()
        cutoff = (today - dt.timedelta(days=since_days)).isoformat()
        year = today.year
        rows = []
        try:
            rows = self._load_index(year)
        except CongressError as exc:
            logger.warning("Congress watch index unavailable (%s); skipping", exc)
            return []
        if today.month == 1:
            try:
                rows += self._load_index(year - 1)
            except CongressError:
                pass

        recent = [r for r in rows if r["type"] == "P" and (r["filed"] or "") >= cutoff]
        if not recent or not read_pdfs:
            return []

        matches = []
        for rep in self._read_ptrs(recent, year):
            hits = [t for t in rep["tickers"] if t in symbols]
            if hits:
                rep["matched_tickers"] = hits
                matches.append(rep)
        return matches


class CongressError(Exception):
    """Raised when the House Clerk index cannot be reached or parsed."""
