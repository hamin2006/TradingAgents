"""market_tape.py — regime/tape context line for the analyst context block.

The 2026-09-03 audit showed per-ticker debates arguing market/regime blind:
REGN's bull cited "PFE at 52-week highs, healthcare defensive" from news
articles rather than data. This module renders one compact, dated line:

    Market tape: SPY 620.00 above its 200d SMA (600.00); VIX 14.2; XLV
    (Healthcare) -0.8% over the last session.

Every piece is failure-safe (a broken fetch drops that clause, not the whole
line); builders are memoized per day because all 16 tickers share one tape.
"""

from __future__ import annotations

import threading
import time

import yfinance as yf

SECTOR_ETF = {
    "Healthcare": "XLV",
    "Technology": "XLK",
    "Financials": "XLF",
    "Consumer Defensive": "XLP",
    "Consumer Cyclical": "XLY",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Communication Services": "XLC",
    "Real Estate": "XLRE",
}

_TTL_S = 600
_memo: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def reset_tape_cache() -> None:
    with _lock:
        _memo.clear()


def _closes(symbol: str, period: str = "7d"):
    """Recent close series for a symbol (monkeypatch seam)."""
    hist = yf.Ticker(symbol).history(period=period, interval="1d")
    if hist is None or hist.empty:
        return None
    return hist["Close"].astype(float)


def _spy_tape() -> dict:
    closes = _closes("SPY", period="1y")
    if closes is None or len(closes) < 2:
        raise OSError("no SPY data")
    sma200 = None
    if len(closes) >= 200:
        sma200 = float(closes.iloc[-200:].mean())
    return {"close": float(closes.iloc[-1]), "sma200": sma200}


def _vix_tape() -> dict:
    closes = _closes("^VIX", period="7d")
    if closes is None or len(closes) < 1:
        raise OSError("no VIX data")
    return {"close": float(closes.iloc[-1])}


def _etf_change(etf: str) -> float | None:
    closes = _closes(etf, period="7d")
    if closes is None or len(closes) < 2:
        return None
    return (closes.iloc[-1] / closes.iloc[-2] - 1) * 100


def tape_line(sector: str | None = None) -> str:
    """One dated market-tape line; "" on any failure."""
    key = sector or "global"
    with _lock:
        hit = _memo.get(key)
        if hit is not None and time.monotonic() - hit[0] < _TTL_S:
            return hit[1]
    parts: list[str] = []
    try:
        spy = _spy_tape()
        rel = ""
        if spy.get("sma200"):
            direction = "above" if spy["close"] >= spy["sma200"] else "below"
            rel = f" {direction} its 200d SMA ({spy['sma200']:,.2f})"
        parts.append(f"SPY {spy['close']:,.2f}{rel}")
    except Exception:  # noqa: BLE001 - tape is decoration
        pass
    try:
        vix = _vix_tape()
        parts.append(f"VIX {vix['close']:.1f}")
    except Exception:  # noqa: BLE001
        pass
    if sector and sector in SECTOR_ETF:
        try:
            pct = _etf_change(SECTOR_ETF[sector])
            if pct is not None:
                parts.append(f"{SECTOR_ETF[sector]} ({sector}) {pct:+.1f}%")
        except Exception:  # noqa: BLE001
            pass
    line = ("Market tape: " + "; ".join(parts) + ".") if parts else ""
    with _lock:
        _memo[key] = (time.monotonic(), line)
    return line
