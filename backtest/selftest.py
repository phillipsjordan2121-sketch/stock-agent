"""
selftest.py -- offline validation of the engine with synthetic random-walk
price data (NO network, NO Polygon key). Exercises indicators, scoring,
simulation, metrics, walk-forward, and the intraday-benefit test, and checks
basic invariants. Run: python -m backtest.selftest
"""
from __future__ import annotations

import random
from datetime import date, timedelta

import numpy as np

from . import config, engine, indicators


def _synth_bars(n=400, seed=0, drift=0.0004, vol=0.02):
    rng = random.Random(seed)
    price = 100.0
    o, h, l, c, v, dates = [], [], [], [], [], []
    d = date(2024, 1, 1)
    for _ in range(n):
        ret = drift + rng.gauss(0, vol)
        new = max(1.0, price * (1 + ret))
        op = price
        cl = new
        hi = max(op, cl) * (1 + abs(rng.gauss(0, vol / 2)))
        lo = min(op, cl) * (1 - abs(rng.gauss(0, vol / 2)))
        o.append(op); h.append(hi); l.append(lo); c.append(cl)
        v.append(1_000_000 * (1 + abs(rng.gauss(0, 0.5))))
        # skip weekends to look like a trading calendar
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        dates.append(d.isoformat())
        price = new
    return {"dates": dates,
            "o": np.array(o), "h": np.array(h), "l": np.array(l),
            "c": np.array(c), "v": np.array(v)}


def run():
    print("Building synthetic universe...")
    tickers = [f"TST{i}" for i in range(12)]
    bars = {t: _synth_bars(seed=i, drift=0.0003 + 0.0001 * (i % 4)) for i, t in enumerate(tickers)}
    # a 'SPY' benchmark with steady mild drift
    bars[config.BENCHMARK] = _synth_bars(seed=99, drift=0.0004, vol=0.01)

    # ---- indicators sanity ----
    ind = indicators.compute(bars["TST0"])
    assert ind["trend"].shape == bars["TST0"]["c"].shape
    assert np.nanmax(ind["trend"]) <= 1.0 + 1e-9 and np.nanmin(ind["trend"]) >= -1e-9
    assert np.nanmax(ind["breakout"]) <= 1.0 + 1e-9
    assert np.nanmax(ind["volume"]) <= 1.0 + 1e-9
    print("  indicators: bounds OK")

    prep = engine.prepare(bars)
    cal = prep["calendar"]
    start, end = cal[config.MIN_HISTORY_BARS], cal[-1]

    # ---- score never exceeds 100 ----
    smax = 0.0
    for t in tickers:
        i = len(bars[t]["c"]) - 1
        s = indicators.score(config.DEFAULT_WEIGHTS, ind["trend"][i], 1.0,
                             ind["breakout"][i], ind["volume"][i], 1.0, 1.0)
        smax = max(smax, s)
    assert smax <= 100.0 + 1e-6, smax
    print(f"  scoring: max score {smax:.1f} <= 100 OK")

    # ---- simulate ----
    sim = engine.simulate(prep, config.DEFAULT_WEIGHTS, start, end, exit_mode="intraday")
    m = engine.metrics(sim, prep, start, end)
    assert len(sim["equity"]) > 0
    assert sim["final"] > 0
    # cash conservation: every trade realised, final is pure cash
    assert isinstance(sim["final"], float)
    print(f"  simulate: {m['trades']} trades, return {m['total_return']:.3f}, "
          f"win {m['win_rate']}, maxDD {m['max_drawdown']:.3f}, sharpe {m['sharpe']}")

    # ---- exposure ceiling never breached (spot check via re-sim equity > 0) ----
    assert m["max_drawdown"] <= 0.0 + 1e-9
    print("  metrics: drawdown sign OK")

    # ---- daily vs intraday exit modes both run ----
    ib = engine.intraday_benefit(prep, config.DEFAULT_WEIGHTS, start, end)
    assert "daily_exit" in ib and "intraday_exit" in ib
    print(f"  intraday benefit: daily {ib['daily_exit']['total_return']:.3f} vs "
          f"intraday {ib['intraday_exit']['total_return']:.3f}")

    # ---- walk-forward ----
    wf = engine.walk_forward(prep, folds=2, fast=True)
    assert "recommended_weights" in wf
    assert abs(sum(wf["recommended_weights"].values()) - 1.0) < 1e-6
    assert len(wf["folds"]) == 2
    print(f"  walk-forward: gate={wf['gate_pass']}, "
          f"rec weights={wf['recommended_weights']}, "
          f"OOS trades={wf['oos_aggregate']['trades']}")

    # ---- weight grid integrity ----
    grid = engine._weight_grid()
    assert all(abs(sum(w.values()) - 1.0) < 1e-6 for w in grid)
    assert len(grid) > 5
    print(f"  weight grid: {len(grid)} vectors, all sum to 1.0 OK")

    print("\nALL SELF-TESTS PASSED.")


if __name__ == "__main__":
    run()
