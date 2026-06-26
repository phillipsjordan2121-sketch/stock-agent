"""
data.py -- Polygon.io market-data client for the backtest.

Reads ONLY (aggregates + snapshot/reference for probing). The API key is sent
in the Authorization header (never in the URL/query string). Daily bars are
cached to backtest/cache/ so re-runs don't re-pull.
"""
from __future__ import annotations

import os
import time
import json
import urllib.request
import urllib.error
from datetime import date, timedelta

import numpy as np

POLYGON_BASE = "https://api.polygon.io"
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")


def _api_key() -> str:
    key = os.environ.get("POLYGON_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "POLYGON_API_KEY not set. In GitHub Actions this comes from the "
            "repo secret; locally, export it in your shell."
        )
    return key


def _get(path: str, params: dict | None = None, timeout: int = 30, retries: int = 4):
    """GET a Polygon endpoint. Key goes in the Authorization header, not the URL."""
    params = dict(params or {})
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{POLYGON_BASE}{path}" + (f"?{qs}" if qs else "")
    headers = {"Authorization": f"Bearer {_api_key()}"}

    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:                       # rate limited -> back off
                time.sleep(2 * (attempt + 1))
                continue
            if e.code in (401, 403):                # auth/tier problem -> surface
                body = e.read().decode(errors="ignore")[:300]
                raise RuntimeError(f"Polygon {e.code} on {path}: {body}") from e
            time.sleep(1 + attempt)
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(1 + attempt)
    raise RuntimeError(f"Polygon GET failed after {retries} tries: {path} ({last_err})")


# ----------------------------------------------------------------------------
# Probe: confirm the key's tier covers what Phase 0 needs.
# ----------------------------------------------------------------------------

def probe_tier(sample_ticker: str = "AAPL") -> dict:
    """Check aggregates, all-tickers snapshot, and intraday minute bars."""
    out = {"aggregates_daily": False, "snapshot_all": False,
           "intraday_minute": False, "notes": []}

    today = date.today()
    frm = (today - timedelta(days=400)).isoformat()
    to = today.isoformat()

    # 1) daily aggregates over ~1y
    try:
        r = _get(f"/v2/aggs/ticker/{sample_ticker}/range/1/day/{frm}/{to}",
                 {"adjusted": "true", "sort": "asc", "limit": 50000})
        n = r.get("resultsCount", 0)
        out["aggregates_daily"] = n > 100
        out["notes"].append(f"daily aggregates: {n} bars for {sample_ticker}")
    except Exception as e:
        out["notes"].append(f"daily aggregates FAILED: {e}")

    # 2) all-tickers snapshot (the bulk read the live intraday runs will use)
    try:
        r = _get("/v2/snapshot/locale/us/markets/stocks/tickers", {})
        tickers = r.get("tickers", [])
        out["snapshot_all"] = len(tickers) > 100
        out["notes"].append(f"snapshot/all-tickers: {len(tickers)} tickers")
    except Exception as e:
        out["notes"].append(f"snapshot/all-tickers FAILED: {e}")

    # 3) intraday minute bars (for live exit runs; backtest uses daily H/L proxy)
    try:
        ifrm = (today - timedelta(days=5)).isoformat()
        r = _get(f"/v2/aggs/ticker/{sample_ticker}/range/1/minute/{ifrm}/{to}",
                 {"adjusted": "true", "sort": "asc", "limit": 50000})
        n = r.get("resultsCount", 0)
        out["intraday_minute"] = n > 50
        out["notes"].append(f"intraday minute aggregates: {n} bars")
    except Exception as e:
        out["notes"].append(f"intraday minute FAILED: {e}")

    return out


# ----------------------------------------------------------------------------
# Daily aggregates fetch (+ disk cache)
# ----------------------------------------------------------------------------

def _cache_path(ticker: str, frm: str, to: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe = ticker.replace("/", "_")
    return os.path.join(CACHE_DIR, f"{safe}_{frm}_{to}.json")


def fetch_daily(ticker: str, frm: str, to: str, use_cache: bool = True) -> dict | None:
    """
    Daily OHLCV for [frm, to]. Returns a dict of numpy arrays:
      {"dates": [iso...], "o","h","l","c","v": np.ndarray} sorted ascending.
    None if no data.
    """
    cp = _cache_path(ticker, frm, to)
    if use_cache and os.path.exists(cp):
        with open(cp) as f:
            raw = json.load(f)
    else:
        raw = _get(f"/v2/aggs/ticker/{ticker}/range/1/day/{frm}/{to}",
                   {"adjusted": "true", "sort": "asc", "limit": 50000})
        if use_cache:
            with open(cp, "w") as f:
                json.dump(raw, f)

    results = raw.get("results") or []
    if len(results) < 2:
        return None

    dates, o, h, l, c, v = [], [], [], [], [], []
    for bar in results:
        ts = bar.get("t")
        if ts is None:
            continue
        d = date.fromtimestamp(ts / 1000.0)  # Polygon 't' is epoch ms (UTC)
        dates.append(d.isoformat())
        o.append(bar.get("o", bar.get("c", 0.0)))
        h.append(bar.get("h", 0.0))
        l.append(bar.get("l", 0.0))
        c.append(bar.get("c", 0.0))
        v.append(bar.get("v", 0.0))

    return {
        "dates": dates,
        "o": np.asarray(o, dtype=float),
        "h": np.asarray(h, dtype=float),
        "l": np.asarray(l, dtype=float),
        "c": np.asarray(c, dtype=float),
        "v": np.asarray(v, dtype=float),
    }


def fetch_universe(tickers: list[str], frm: str, to: str,
                   pace: float = 0.0, use_cache: bool = True) -> dict:
    """Fetch daily bars for many tickers. Returns {ticker: bars_dict}."""
    out = {}
    n = len(tickers)
    for i, t in enumerate(tickers, 1):
        try:
            bars = fetch_daily(t, frm, to, use_cache=use_cache)
            if bars is not None:
                out[t] = bars
        except Exception as e:
            print(f"  [WARN] fetch_daily({t}) failed: {e}")
        if i % 25 == 0 or i == n:
            print(f"  fetched {i}/{n} tickers ({len(out)} with data)")
        if pace:
            time.sleep(pace)
    return out
