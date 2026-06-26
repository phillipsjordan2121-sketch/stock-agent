"""
engine.py -- bar-by-bar backtest simulation, performance metrics, walk-forward
weight tuning, and the intraday-exit benefit test.

The simulation mirrors the live agent's gating/sizing/exit rules so results are
representative. No live state files are touched.
"""
from __future__ import annotations

import itertools
from datetime import date

import numpy as np

from . import config, indicators


# ============================================================================
# Preparation: indicators + alignment + cross-sectional momentum
# ============================================================================

def prepare(bars_by_ticker: dict) -> dict:
    """Compute indicators, date indexes, momentum ranks, and the master calendar."""
    indis, date_index = {}, {}
    for t, bars in bars_by_ticker.items():
        indis[t] = indicators.compute(bars)
        date_index[t] = {d: i for i, d in enumerate(bars["dates"])}

    mom_rank = indicators.cross_sectional_mom(indis, date_index)

    all_dates = sorted({d for bars in bars_by_ticker.values() for d in bars["dates"]})
    return {
        "bars": bars_by_ticker,
        "indis": indis,
        "date_index": date_index,
        "mom_rank": mom_rank,
        "calendar": all_dates,
    }


def _alloc_fraction(conf: float) -> float:
    for lo, hi, frac in config.CONFIDENCE_ALLOC:
        if lo <= conf <= hi:
            return frac
    return 0.0


# ============================================================================
# Core simulation
# ============================================================================

def simulate(prep: dict, weights: dict, start: str, end: str,
             exit_mode: str = "intraday") -> dict:
    """
    Run the strategy over [start, end].

    exit_mode:
      "intraday" -- stops/targets/trailing fill at the threshold price when the
                    bar's low/high crosses it (proxy for the ~10 intraday runs).
      "daily"    -- those same rules evaluate against the CLOSE only and fill at
                    the close (the old once-a-day cadence).

    Returns {"trades": [...], "equity": [(date, value)...], "final": float}.
    """
    bars = prep["bars"]; indis = prep["indis"]
    di = prep["date_index"]; mom_rank = prep["mom_rank"]
    calendar = [d for d in prep["calendar"] if start <= d <= end]

    cash = config.STARTING_CAPITAL
    positions: dict[str, dict] = {}
    trades: list[dict] = []
    equity_curve: list[tuple[str, float]] = []

    def _mark(d_iso: str) -> float:
        val = cash
        for t, p in positions.items():
            i = di[t].get(d_iso)
            px = bars[t]["c"][i] if i is not None else p["last_price"]
            val += p["shares"] * px
        return val

    for d_iso in calendar:
        cur_idx = {t: di[t].get(d_iso) for t in bars}

        # ---- 1. EXITS on open positions ----
        for t in list(positions.keys()):
            i = cur_idx[t]
            if i is None:
                continue
            p = positions[t]
            o, hi, lo, cl = (bars[t]["o"][i], bars[t]["h"][i],
                             bars[t]["l"][i], bars[t]["c"][i])
            p["last_price"] = cl
            entry = p["entry_price"]
            peak_prior = p["peak"]                       # peak BEFORE today (no lookahead)
            sma20 = indis[t]["sma20"][i]
            held_days = i - p["entry_idx"]

            stop_px = entry * (1 + config.STOP_LOSS_PCT)
            target_px = entry * (1 + config.PROFIT_LOCK_PCT)
            trail_px = peak_prior * (1 + config.TRAILING_DROP_PCT)

            exit_px, reason = None, None
            if exit_mode == "intraday":
                if lo <= stop_px:
                    exit_px, reason = stop_px, "stop_loss"
                elif lo <= trail_px:
                    exit_px, reason = trail_px, "trailing_stop"
                elif config.MOMENTUM_BREAK_ON_MA and not np.isnan(sma20) and cl < sma20:
                    exit_px, reason = cl, "momentum_break"
                elif hi >= target_px:
                    exit_px, reason = target_px, "profit_lock"
                elif held_days >= config.HOLD_DAYS_MAX:
                    exit_px, reason = cl, "hold_expired"
            else:  # daily: evaluate against the close only
                if cl <= stop_px:
                    exit_px, reason = cl, "stop_loss"
                elif config.MOMENTUM_BREAK_ON_MA and not np.isnan(sma20) and cl < sma20:
                    exit_px, reason = cl, "momentum_break"
                elif cl <= trail_px:
                    exit_px, reason = cl, "trailing_stop"
                elif cl >= target_px:
                    exit_px, reason = cl, "profit_lock"
                elif held_days >= config.HOLD_DAYS_MAX:
                    exit_px, reason = cl, "hold_expired"

            # update peak with today's high AFTER computing the trigger
            if hi > p["peak"]:
                p["peak"] = hi

            if exit_px is not None:
                gross = (exit_px - entry) / entry
                net = gross - config.COST_ROUNDTRIP_PCT
                cash += p["shares"] * exit_px
                trades.append({
                    "ticker": t, "entry_date": p["entry_date"], "exit_date": d_iso,
                    "entry_price": round(entry, 4), "exit_price": round(exit_px, 4),
                    "hold_days": held_days, "exit_reason": reason,
                    "pnl_pct_gross": round(gross, 6), "pnl_pct": round(net, 6),
                    "confidence": p["confidence"],
                })
                del positions[t]

        # ---- 2. circuit breaker: pause new entries if down >= 8% from start ----
        equity_now = _mark(d_iso)
        paused = (equity_now - config.STARTING_CAPITAL) / config.STARTING_CAPITAL <= config.PORTFOLIO_STOP_PCT

        # ---- 3. ENTRIES ----
        if not paused:
            candidates = []
            for t in bars:
                i = cur_idx[t]
                if i is None or i < config.MIN_HISTORY_BARS or t in positions:
                    continue
                ind = indis[t]
                sc = indicators.score(
                    weights, ind["trend"][i], mom_rank[t][i],
                    ind["breakout"][i], ind["volume"][i],
                    ind["rsi_mult"][i], ind["macd_mult"][i],
                )
                if sc >= config.CONFIDENCE_THRESHOLD:
                    candidates.append((sc, t, i))
            candidates.sort(reverse=True)

            for sc, t, i in candidates:
                equity = _mark(d_iso)
                invested = sum(positions[x]["shares"] * bars[x]["c"][di[x][d_iso]]
                               for x in positions if di[x].get(d_iso) is not None)
                conf = int(round(sc))
                frac = min(_alloc_fraction(conf), config.MAX_SINGLE_POSITION_PCT)
                target = equity * frac
                if invested + target > config.EXPOSURE_CEILING * equity:
                    continue
                target = min(target, cash)
                if target < 1:
                    continue
                price = bars[t]["c"][i]
                shares = target / price
                cash -= shares * price
                positions[t] = {
                    "shares": shares, "entry_price": price, "entry_date": d_iso,
                    "entry_idx": i, "peak": price, "last_price": price,
                    "confidence": conf,
                }

        equity_curve.append((d_iso, _mark(d_iso)))

    # ---- close out anything still open at the final bar ----
    if calendar:
        last = calendar[-1]
        for t in list(positions.keys()):
            i = di[t].get(last)
            p = positions[t]
            px = bars[t]["c"][i] if i is not None else p["last_price"]
            gross = (px - p["entry_price"]) / p["entry_price"]
            net = gross - config.COST_ROUNDTRIP_PCT
            cash += p["shares"] * px
            trades.append({
                "ticker": t, "entry_date": p["entry_date"], "exit_date": last,
                "entry_price": round(p["entry_price"], 4), "exit_price": round(px, 4),
                "hold_days": (i - p["entry_idx"]) if i is not None else 0,
                "exit_reason": "end_of_test",
                "pnl_pct_gross": round(gross, 6), "pnl_pct": round(net, 6),
                "confidence": p["confidence"],
            })
            del positions[t]
        equity_curve[-1] = (last, cash)

    return {"trades": trades, "equity": equity_curve, "final": cash}


# ============================================================================
# Metrics
# ============================================================================

def benchmark_return(prep: dict, start: str, end: str, symbol: str = None) -> float | None:
    """SPY (or given symbol) buy-and-hold return over [start, end]."""
    symbol = symbol or config.BENCHMARK
    bars = prep["bars"].get(symbol)
    if not bars:
        return None
    ds = [d for d in bars["dates"] if start <= d <= end]
    if len(ds) < 2:
        return None
    i0 = bars["dates"].index(ds[0]); i1 = bars["dates"].index(ds[-1])
    c0, c1 = bars["c"][i0], bars["c"][i1]
    return (c1 - c0) / c0 if c0 else None


def metrics(sim: dict, prep: dict, start: str, end: str) -> dict:
    trades = sim["trades"]
    eq = np.array([v for _, v in sim["equity"]], dtype=float)
    n = len(trades)
    wins = [t for t in trades if t["pnl_pct"] > 0]
    pnls = np.array([t["pnl_pct"] for t in trades]) if n else np.array([])

    total_return = (sim["final"] - config.STARTING_CAPITAL) / config.STARTING_CAPITAL

    # max drawdown
    if len(eq):
        peak = np.maximum.accumulate(eq)
        dd = (eq - peak) / peak
        max_dd = float(dd.min())
    else:
        max_dd = 0.0

    # daily Sharpe (annualised)
    if len(eq) > 2:
        rets = np.diff(eq) / eq[:-1]
        sharpe = float(np.sqrt(252) * rets.mean() / rets.std()) if rets.std() > 0 else 0.0
    else:
        sharpe = 0.0

    spy = benchmark_return(prep, start, end)

    return {
        "trades": n,
        "win_rate": round(len(wins) / n, 4) if n else None,
        "avg_pnl_pct": round(float(pnls.mean()), 6) if n else None,
        "expectancy": round(float(pnls.mean()), 6) if n else None,
        "total_return": round(total_return, 6),
        "max_drawdown": round(max_dd, 6),
        "sharpe": round(sharpe, 4),
        "spy_return": round(spy, 6) if spy is not None else None,
        "vs_spy": round(total_return - spy, 6) if spy is not None else None,
        "start": start, "end": end,
    }


# ============================================================================
# Walk-forward weight tuning
# ============================================================================

def _weight_grid() -> list[dict]:
    vals = config.WEIGHT_GRID_VALUES
    grid = []
    for combo in itertools.product(vals, repeat=len(config.SIGNAL_KEYS)):
        if abs(sum(combo) - 1.0) < 1e-6:
            grid.append(dict(zip(config.SIGNAL_KEYS, combo)))
    return grid


def _fold_windows(calendar: list[str], folds: int) -> list[tuple]:
    """Rolling folds: train on segment k, validate on segment k+1."""
    n = len(calendar)
    seg = n // (folds + 1)
    out = []
    for k in range(folds):
        is_lo, is_hi = k * seg, (k + 1) * seg - 1
        oos_lo, oos_hi = (k + 1) * seg, (k + 2) * seg - 1 if k < folds - 1 else n - 1
        out.append((calendar[is_lo], calendar[is_hi],
                    calendar[oos_lo], calendar[oos_hi]))
    return out


def walk_forward(prep: dict, folds: int = None, fast: bool = False) -> dict:
    """
    For each fold: grid-search weights on the in-sample window (pick robust
    positive-expectancy vector with enough trades), then validate out-of-sample.
    Aggregates OOS results -- that's the GATE evidence.
    """
    folds = folds or config.WALK_FORWARD_FOLDS
    grid = _weight_grid()
    if fast:
        grid = grid[:: max(1, len(grid) // 12)]  # thin the grid for speed
    windows = _fold_windows(prep["calendar"], folds)

    fold_reports = []
    all_oos_trades = []
    oos_spans = []

    for fi, (is0, is1, oos0, oos1) in enumerate(windows, 1):
        best = None
        for w in grid:
            sim = simulate(prep, w, is0, is1, exit_mode="intraday")
            m = metrics(sim, prep, is0, is1)
            if m["trades"] < config.MIN_IS_TRADES or m["expectancy"] is None:
                continue
            key = (m["expectancy"], m["sharpe"])
            if best is None or key > best["key"]:
                best = {"key": key, "weights": w, "is_metrics": m}
        if best is None:                       # nothing traded enough -> default
            best = {"weights": dict(config.DEFAULT_WEIGHTS), "is_metrics": None}

        oos_sim = simulate(prep, best["weights"], oos0, oos1, exit_mode="intraday")
        oos_m = metrics(oos_sim, prep, oos0, oos1)
        all_oos_trades += oos_sim["trades"]
        oos_spans.append((oos0, oos1))
        fold_reports.append({
            "fold": fi, "is": [is0, is1], "oos": [oos0, oos1],
            "weights": best["weights"],
            "is_metrics": best["is_metrics"], "oos_metrics": oos_m,
        })

    # aggregate OOS
    agg = _aggregate_oos(all_oos_trades, prep, oos_spans)
    # final recommended weights = the most recent fold's selection
    rec = fold_reports[-1]["weights"] if fold_reports else dict(config.DEFAULT_WEIGHTS)
    gate = (agg["expectancy"] is not None and agg["expectancy"] > 0
            and agg["vs_spy"] is not None and agg["vs_spy"] > 0)
    return {"folds": fold_reports, "oos_aggregate": agg,
            "recommended_weights": rec, "gate_pass": gate}


def _aggregate_oos(trades: list, prep: dict, spans: list) -> dict:
    n = len(trades)
    pnls = np.array([t["pnl_pct"] for t in trades]) if n else np.array([])
    wins = [t for t in trades if t["pnl_pct"] > 0]
    # compound the per-trade pnl as a rough OOS return proxy, and sum SPY over spans
    strat_ret = float(np.prod(1 + pnls) - 1) if n else 0.0
    spy_ret = 0.0
    for s0, s1 in spans:
        b = benchmark_return(prep, s0, s1)
        if b is not None:
            spy_ret = (1 + spy_ret) * (1 + b) - 1
    return {
        "trades": n,
        "win_rate": round(len(wins) / n, 4) if n else None,
        "expectancy": round(float(pnls.mean()), 6) if n else None,
        "compound_return": round(strat_ret, 6),
        "spy_return": round(spy_ret, 6),
        "vs_spy": round(strat_ret - spy_ret, 6),
    }


# ============================================================================
# Intraday-exit benefit test
# ============================================================================

def intraday_benefit(prep: dict, weights: dict, start: str, end: str) -> dict:
    """Same weights, two exit cadences -> quantify the intraday exit benefit."""
    daily = simulate(prep, weights, start, end, exit_mode="daily")
    intra = simulate(prep, weights, start, end, exit_mode="intraday")
    md = metrics(daily, prep, start, end)
    mi = metrics(intra, prep, start, end)
    return {
        "daily_exit": md,
        "intraday_exit": mi,
        "return_improvement": round((mi["total_return"] or 0) - (md["total_return"] or 0), 6),
        "expectancy_improvement": round((mi["expectancy"] or 0) - (md["expectancy"] or 0), 6),
    }
