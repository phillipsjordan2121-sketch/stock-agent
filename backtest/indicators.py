"""
indicators.py -- deterministic technical indicators + the linear, per-signal
attributable scoring model.

All indicators are computed with NO lookahead: the value at bar i uses only
bars <= i (and for "prior" windows, strictly < i).
"""
from __future__ import annotations

import numpy as np

from . import config


def _sma(arr: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(arr, np.nan, dtype=float)
    if len(arr) < n:
        return out
    csum = np.cumsum(np.insert(arr, 0, 0.0))
    out[n - 1:] = (csum[n:] - csum[:-n]) / n
    return out


def _ema(arr: np.ndarray, span: int) -> np.ndarray:
    out = np.full_like(arr, np.nan, dtype=float)
    if len(arr) == 0:
        return out
    alpha = 2.0 / (span + 1.0)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def _rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    out = np.full_like(close, np.nan, dtype=float)
    if len(close) <= n:
        return out
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = gain[:n].mean()
    avg_loss = loss[:n].mean()
    for i in range(n, len(close)):
        g = gain[i - 1]
        l = loss[i - 1]
        avg_gain = (avg_gain * (n - 1) + g) / n
        avg_loss = (avg_loss * (n - 1) + l) / n
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def _prior_rolling_max(arr: np.ndarray, n: int) -> np.ndarray:
    """Max of the n bars STRICTLY before i (excludes bar i)."""
    out = np.full_like(arr, np.nan, dtype=float)
    for i in range(n, len(arr)):
        out[i] = arr[i - n:i].max()
    return out


def _prior_rolling_mean(arr: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(arr, np.nan, dtype=float)
    for i in range(n, len(arr)):
        out[i] = arr[i - n:i].mean()
    return out


def compute(bars: dict) -> dict:
    """
    Compute all indicator arrays for one ticker's bars.
    Returns a dict of numpy arrays aligned to bars["dates"].
    """
    c = bars["c"]
    h = bars["h"]
    v = bars["v"]
    n = len(c)

    sma20 = _sma(c, 20)
    sma50 = _sma(c, 50)

    sma20_rising = np.zeros(n, dtype=bool)
    sma50_rising = np.zeros(n, dtype=bool)
    sma20_rising[5:] = sma20[5:] > sma20[:-5]
    sma50_rising[5:] = sma50[5:] > sma50[:-5]
    sma20_rising &= ~np.isnan(sma20)
    sma50_rising &= ~np.isnan(sma50)

    high20_prior = _prior_rolling_max(h, 20)
    avgvol20_prior = _prior_rolling_mean(v, 20)

    ret5 = np.full(n, np.nan)
    ret20 = np.full(n, np.nan)
    ret5[5:] = c[5:] / c[:-5] - 1.0
    ret20[20:] = c[20:] / c[:-20] - 1.0

    rsi = _rsi(c, 14)
    macd_line = _ema(c, 12) - _ema(c, 26)

    # --- signal sub-scores, each normalised to [0, 1] ---
    with np.errstate(invalid="ignore"):
        trend = (
            (c > sma20).astype(float)
            + (c > sma50).astype(float)
            + sma20_rising.astype(float)
            + sma50_rising.astype(float)
        ) / 4.0
        # zero-out trend where the 50d MA isn't defined yet
        trend = np.where(np.isnan(sma50), np.nan, trend)

        ratio = np.where(high20_prior > 0, c / high20_prior, np.nan)
        breakout = np.clip((ratio - 0.95) / 0.05, 0.0, 1.0)

        vol_ratio = np.where(avgvol20_prior > 0, v / avgvol20_prior, np.nan)
        volume = np.clip(vol_ratio / 2.0, 0.0, 1.0)

        rsi_mult = np.where(
            np.isnan(rsi), 1.0,
            np.where(rsi < 75, 1.0, np.clip(1.0 - (rsi - 75) / 20.0, 0.5, 1.0)),
        )
        macd_mult = np.where(np.isnan(macd_line), 1.0,
                             np.where(macd_line > 0, 1.0, 0.85))

    # blended raw momentum (used for the cross-sectional rank elsewhere)
    _stack = np.vstack([ret5, ret20])
    _cnt = (~np.isnan(_stack)).sum(axis=0)
    blended_mom = np.where(_cnt > 0, np.nansum(_stack, axis=0) / np.maximum(_cnt, 1), np.nan)

    return {
        "sma20": sma20, "sma50": sma50,
        "trend": trend, "breakout": breakout, "volume": volume,
        "blended_mom": blended_mom,
        "rsi": rsi, "rsi_mult": rsi_mult,
        "macd_line": macd_line, "macd_mult": macd_mult,
    }


def cross_sectional_mom(indis: dict, date_index: dict) -> dict:
    """
    Per-date percentile rank (0..1) of blended momentum across the universe.
    Returns {ticker: np.ndarray} aligned to each ticker's bars.

    date_index: {ticker: {date_iso: i}} for alignment.
    """
    # gather all (date -> list of (ticker, value))
    by_date: dict[str, list] = {}
    for t, ind in indis.items():
        bm = ind["blended_mom"]
        di = date_index[t]
        for d_iso, i in di.items():
            val = bm[i]
            if not np.isnan(val):
                by_date.setdefault(d_iso, []).append((t, val))

    mom_rank = {t: np.full(len(ind["blended_mom"]), np.nan)
                for t, ind in indis.items()}
    for d_iso, pairs in by_date.items():
        if len(pairs) < 3:
            continue
        vals = np.array([p[1] for p in pairs])
        order = vals.argsort()
        ranks = np.empty(len(vals))
        ranks[order] = np.arange(len(vals))
        pct = ranks / (len(vals) - 1)         # 0 (worst) .. 1 (best)
        for (t, _), p in zip(pairs, pct):
            mom_rank[t][date_index[t][d_iso]] = p
    return mom_rank


def score(weights: dict, trend, mom, breakout, volume, rsi_mult, macd_mult) -> float:
    """
    Linear weighted score -> 0..100, then guard multipliers (RSI/MACD).
    Returns -1.0 if any core signal is undefined (name not tradeable yet).
    """
    if np.isnan(trend) or np.isnan(mom) or np.isnan(breakout) or np.isnan(volume):
        return -1.0
    base = (weights["trend"] * trend
            + weights["momentum"] * mom
            + weights["breakout"] * breakout
            + weights["volume"] * volume)
    return float(100.0 * base * rsi_mult * macd_mult)
