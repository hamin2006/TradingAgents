"""edgar.py — SEC EDGAR client (companyfacts + submissions), as-of semantics.

Read-only consumption of SEC public APIs (no key): companyfacts XBRL data and
the submissions index. Everything the framework's fundamentals hybrid and the
corporate-events feed need: CIK resolution, per-ticker companyfacts with
point-in-time filtering (``filed <= as-of`` — no restatement lookahead),
amendment dedupe, fiscal-quarter TTM math, tag fallback chains, and computed
metrics (EBITDA, FCF, buybacks, dividends, shares).

SEC etiquette: descriptive User-Agent, paced requests (>= 1s apart, shared
RLock — the analyze run fans out over 4 worker threads). All fetches go
through the module-level ``_http_get`` seam so hermetic tests inject fixtures
without network. Companyfacts/submissions payloads cache to disk per CIK.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

UA = "TradingAgents research contact@example.com"
DATA_BASE = "https://data.sec.gov"
# The ticker map is served from www.sec.gov/files (data.sec.gov 404s it).
_CIK_URL = "https://www.sec.gov/files/company_tickers.json"
_FACTS_URL = DATA_BASE + "/api/xbrl/companyfacts/CIK{}.json"
_SUBMISSIONS_URL = DATA_BASE + "/submissions/CIK{}.json"
_CACHE_ENV = "EDGAR_CACHE_DIR"
_MIN_INTERVAL_S = 1.0
_CACHE_TTL_S = 24 * 3600

_Q_FRAME = re.compile(r"^CY(\d{4})Q([1-4])$")

class EdgarError(Exception):
    """EDGAR fetch/parse failure (callers fall back, never crash the run)."""


def _jb(payload) -> bytes:
    return json.dumps(payload).encode()


_request_lock = threading.Lock()
_last_request_at: float = 0.0
_MAX_ATTEMPTS = 3


def _pace() -> None:
    """SEC fair access: >=1s between requests, module-wide (worker threads
    share the lock, so 4 parallel analyze workers stay at one request/s)."""
    global _last_request_at
    with _request_lock:
        wait = _MIN_INTERVAL_S - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _http_get_impl(url: str) -> bytes:
    """The actual HTTP request (transport seam for hermetic tests)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _http_get(url: str) -> bytes:
    """Fetch a URL with SEC etiquette and retry-on-throttle backoff.

    Retries HTTP 429/403/5xx and transient network errors (the SEC throttles
    rather than blocks; one 429 must not kill a whole run's ticker map).
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        _pace()
        try:
            return _http_get_impl(url)
        except urllib.error.HTTPError as exc:
            last_exc = exc
            retriable = exc.code in (429, 403) or 500 <= exc.code < 600
            if not retriable or attempt == _MAX_ATTEMPTS - 1:
                break
        except Exception as exc:  # noqa: BLE001 - transient network errors
            last_exc = exc
            if attempt == _MAX_ATTEMPTS - 1:
                break
        time.sleep(3 * (attempt + 1))
    raise EdgarError(
        f"EDGAR fetch failed for {url}: {last_exc}") from last_exc


def _cache_dir() -> Path:
    return Path(os.environ.get(_CACHE_ENV, Path.home() / ".tradingagents" / "cache" / "edgar"))


def clear_cache() -> None:
    """Drop in-memory caches (tests)."""
    _cik_cache.clear()


def _reset_pacing() -> None:
    """Zero the pacing clock (tests)."""
    global _last_request_at
    with _request_lock:
        _last_request_at = 0.0


def _cache_read(kind: str, key: str, ttl: float | None = _CACHE_TTL_S) -> bytes | None:
    path = _cache_dir() / f"{kind}-{key}.json"
    try:
        if ttl is not None and path.stat().st_mtime < time.time() - ttl:
            return None
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _cache_write(kind: str, key: str, body: bytes) -> None:
    try:
        path = _cache_dir() / f"{kind}-{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.rename(path)
    except OSError:
        pass  # cache is best-effort


_cik_cache: dict[str, str] = {}


def _cik_map() -> dict[str, str]:
    """ticker (upper) -> zero-padded CIK; loaded once per process.

    SEC has served company_tickers.json as both an array of rows and an
    object keyed by index — accept either shape.
    """
    if not _cik_cache:
        try:
            payload = json.loads(_http_get(_CIK_URL))
        except (EdgarError, json.JSONDecodeError) as exc:
            raise EdgarError(f"could not load EDGAR ticker map: {exc}") from exc
        rows = payload.values() if isinstance(payload, dict) else payload
        for row in rows:
            _cik_cache[str(row["ticker"]).upper()] = str(row["cik_str"]).zfill(10)
    return _cik_cache


def resolve_cik(ticker: str) -> str:
    """CIK for a ticker (zero-padded, dashless)."""
    cik = _cik_map().get(ticker.strip().upper())
    if not cik:
        raise EdgarError(f"no EDGAR CIK for ticker {ticker}")
    return cik


def dashless(accession: str) -> str:
    """Archives S3 keys use dashless accessions (the API serves them with
    dashes: '0001663758-26-000002' -> '000166375826000002')."""
    return accession.replace("-", "")


def _load_json(kind: str, url: str) -> dict:
    key = url.rsplit("/", 1)[-1].replace(".json", "")
    body = _cache_read(kind, key)
    if body is None:
        body = _http_get(url)
        _cache_write(kind, key, body)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise EdgarError(f"EDGAR returned non-JSON for {url}") from exc


class Facts:
    """Point-in-time view over a company's companyfacts payload."""

    def __init__(self, raw: dict):
        self._facts = raw.get("facts", {})
        self._us_gaap = self._facts.get("us-gaap", {})
        self._dei = self._facts.get("dei", {})

    # -- row access ----------------------------------------------------------

    def _rows(self, tags, namespace="us-gaap", unit="USD"):
        table = self._us_gaap if namespace == "us-gaap" else self._dei
        if isinstance(tags, str):
            tags = [tags]
        # Prefer the candidate with the newest coverage: live PFE class
        # (2026-09-04 QA) — the company switched revenue tags after 2023, so
        # 'first tag with any rows' served stale 2022-era numbers while the
        # current tag sat second in the chain. Ties keep chain order.
        best_rows: list | None = None
        best_end = ""
        for tag in tags:
            entries = table.get(tag, {}).get("units", {}).get(unit)
            if not entries:
                continue
            rows = list(entries)
            if best_rows is None:
                best_rows, best_end = rows, self._max_row_end(rows)
                continue
            end = self._max_row_end(rows)
            if end > best_end:
                best_rows, best_end = rows, end
        return best_rows

    @staticmethod
    def _max_row_end(rows: list) -> str:
        ends = [str(r.get("end") or r.get("start") or "") for r in rows]
        return max(ends) if ends else ""

    def _as_of_ok(self, row, as_of: str) -> bool:
        if row.get("filed", "") > as_of:
            return False
        end = row.get("end") or row.get("start")
        return end is None or end <= as_of

    @staticmethod
    def _quarter_key(row) -> tuple[int, int] | None:
        frame = row.get("frame") or ""
        m = _Q_FRAME.match(frame)
        if m:
            return int(m.group(1)), int(m.group(2))
        end = row.get("end")
        start = row.get("start")
        if not end or start is None:
            return None
        try:
            start_dt = datetime.strptime(start, "%Y-%m-%d")
            end_dt = datetime.strptime(end, "%Y-%m-%d")
        except ValueError:
            return None
        # Frame-less rows: only true quarter-length durations qualify — a 10-K
        # full-year row ending Dec-31 must NOT masquerade as a Q4 quarter
        # (real REGN companyfacts carry both; the annual row is ~4x bigger).
        days = (end_dt - start_dt).days
        if days > 120:
            return None
        return end_dt.year, (end_dt.month - 1) // 3 + 1

    # -- quarterly duration facts -------------------------------------------

    def quarters(self, tags, as_of: str, unit="USD"):
        """Fiscal-quarter duration rows as of ``as_of`` (future filings
        excluded), deduped per quarter by latest ``filed``."""
        rows = self._rows(tags, unit=unit) or []
        by_key: dict[tuple[int, int], dict] = {}
        for row in rows:
            if row.get("start") is None:
                continue  # duration only
            key = self._quarter_key(row)
            if key is None or not self._as_of_ok(row, as_of):
                continue
            prev = by_key.get(key)
            if prev is None or row["filed"] >= prev["filed"]:
                by_key[key] = row
        return [by_key[k] for k in sorted(by_key)]

    def ttm(self, tags, as_of: str, unit="USD") -> float | None:
        """Trailing twelve months: sum of the last 4 fiscal quarters on file
        (fewer is allowed for young filers); None when no quarters exist."""
        rows = self.quarters(tags, as_of, unit)
        if not rows:
            return None
        return round(float(sum(r["val"] for r in rows[-4:])), 2)

    # -- instant facts -------------------------------------------------------

    def latest_instant(self, tags, as_of: str, unit="USD",
                       namespace="us-gaap") -> float | None:
        rows = self._rows(tags, namespace=namespace, unit=unit) or []
        best = None
        for row in rows:
            if row.get("start") is not None:
                continue  # instant only
            if not self._as_of_ok(row, as_of):
                continue
            if best is None or (row.get("end") or "") >= (best.get("end") or ""):
                best = row
        return None if best is None else float(best["val"])

    def shares_outstanding(self, as_of: str) -> int | None:
        for tags, ns in ((["EntityCommonStockSharesOutstanding"], "dei"),
                         (["CommonStockSharesOutstanding"], "us-gaap")):
            val = self.latest_instant(tags, as_of, unit="shares", namespace=ns)
            if val is not None:
                return int(val)
        # Filers like EL never tag an outstanding-shares instant; the diluted
        # weighted average (quarterly duration) is the closest coverage.
        best = None
        for row in (self._rows("WeightedAverageNumberOfDilutedSharesOutstanding",
                               unit="shares") or []):
            if row.get("filed", "") > as_of:
                continue
            if best is None or (row.get("end") or "") >= (best.get("end") or ""):
                best = row
        return None if best is None else int(best["val"])


def load_facts(ticker: str) -> Facts:
    """Companyfacts for a ticker (disk-cached per CIK)."""
    cik = resolve_cik(ticker)
    raw = _load_json("facts", _FACTS_URL.format(cik))
    return Facts(raw)


class Submissions:
    def __init__(self, raw: dict):
        self.cik = str(raw.get("cik", "")).zfill(10)
        self._recent = raw.get("filings", {}).get("recent", {})

    def recent(self, since: str) -> list[dict]:
        """Filings filed on/after ``since`` (YYYY-MM-DD), newest last."""
        out = []
        for i, accn in enumerate(self._recent.get("accessionNumber", [])):
            filed = self._recent.get("filingDate", [])[i] if i < len(
                self._recent.get("filingDate", [])) else ""
            if filed >= since:
                out.append({
                    "accession_number": accn,
                    "form": self._recent.get("form", [])[i] if i < len(
                        self._recent.get("form", [])) else "",
                    "filing_date": filed,
                    "primary_document": self._recent.get("primaryDocument", [])[
                        i] if i < len(self._recent.get("primaryDocument", [])) else "",
                })
        return out


def load_submissions(ticker: str) -> Submissions:
    """Submissions index for a ticker (disk-cached per CIK)."""
    cik = resolve_cik(ticker)
    raw = _load_json("subs", _SUBMISSIONS_URL.format(cik))
    return Submissions(raw)


def now_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
