"""
config.py -- Phase 0 backtest configuration.

Mirrors the live strategy's gating/sizing constants so the backtest reflects
how the real agent would size and exit, and defines the technical-core signal
weights + walk-forward grid.
"""
from __future__ import annotations

# ----------------------------------------------------------------------------
# Universe: single-sourced from the live watchlist so the backtest tracks the
# same names. Falls back to a small list if main.py can't be imported (e.g. the
# `anthropic` dependency is missing in a minimal env / self-test).
# ----------------------------------------------------------------------------
try:
    from main import SECTOR_MAP, WATCHLIST  # type: ignore
except Exception:  # pragma: no cover - fallback only
    SECTOR_MAP = {
        "NVDA": "semiconductor", "AMD": "semiconductor", "AVGO": "semiconductor",
        "MSFT": "cloud_ai", "GOOGL": "cloud_ai", "AMZN": "cloud_ai",
        "META": "cloud_ai", "CRM": "software", "PLTR": "software",
        "PANW": "cybersecurity", "CRWD": "cybersecurity", "TSLA": "ev_auto_tech",
    }
    WATCHLIST = list(SECTOR_MAP.keys())

BENCHMARK = "SPY"

# ----------------------------------------------------------------------------
# Backtest window / data
# ----------------------------------------------------------------------------
DEFAULT_YEARS = 2          # years of daily history to pull
MIN_HISTORY_BARS = 50      # need >=50 bars before a name is tradeable (50d MA)

# ----------------------------------------------------------------------------
# Entry gating + sizing -- mirrors main.py so backtest == live behaviour
# ----------------------------------------------------------------------------
CONFIDENCE_THRESHOLD    = 70
EXPOSURE_CEILING        = 0.35
MAX_SINGLE_POSITION_PCT = 0.10
STARTING_CAPITAL        = 100_000.0

# confidence band -> target allocation fraction of total equity (from main.py)
CONFIDENCE_ALLOC = [
    (90, 100, 0.08),
    (80,  89, 0.06),
    (70,  79, 0.04),
    (60,  69, 0.02),
]

# ----------------------------------------------------------------------------
# Exit rules -- the NEW momentum-fit set from the upgrade plan
# ----------------------------------------------------------------------------
STOP_LOSS_PCT      = -0.07   # hard stop from entry
PROFIT_LOCK_PCT    =  0.10   # NEW default +10% (was +25%)
TRAILING_DROP_PCT  = -0.05   # trailing stop from peak
PORTFOLIO_STOP_PCT = -0.08   # circuit breaker (pause new entries)
HOLD_DAYS_MAX      = 10       # max trading-day hold (extended 5->10 to let winners run)
MOMENTUM_BREAK_ON_MA = True   # exit if close loses the 20-day MA

# ----------------------------------------------------------------------------
# Cost / slippage haircut applied to every round-trip trade (the GATE is net
# of this). 0.0020 = 20 bps round trip (~10 bps/side).
# ----------------------------------------------------------------------------
COST_ROUNDTRIP_PCT = 0.0020

# ----------------------------------------------------------------------------
# Technical-core signal weights (must sum to 1.0). These are the STARTING
# weights; walk-forward tunes them on in-sample windows and validates OOS.
# ----------------------------------------------------------------------------
DEFAULT_WEIGHTS = {
    "trend":    0.30,
    "momentum": 0.35,
    "breakout": 0.20,
    "volume":   0.15,
}
SIGNAL_KEYS = ["trend", "momentum", "breakout", "volume"]

# Walk-forward weight grid: every combo of these per-signal values that sums to
# 1.0 (within tolerance). Coarse on purpose -- we want robust regions, not a
# curve-fit peak.
WEIGHT_GRID_VALUES = [0.0, 0.15, 0.25, 0.35]

# Minimum in-sample trades before a weight vector is eligible to be selected
# (guards against picking a lucky vector that barely traded).
MIN_IS_TRADES = 20

# Number of walk-forward folds (rolling: train on fold k, validate on k+1).
WALK_FORWARD_FOLDS = 3
