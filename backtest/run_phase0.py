"""
run_phase0.py -- Phase 0 entrypoint.

Pulls Polygon history for the universe, runs the walk-forward backtest + the
intraday-exit benefit test, prints a summary, and writes results into
backtest/results/. Writes NOTHING outside backtest/ -- the live agent is untouched.

Usage:
  python -m backtest.run_phase0 --probe          # just confirm the Polygon tier
  python -m backtest.run_phase0 --years 2         # full backtest
  python -m backtest.run_phase0 --years 2 --fast  # thinned grid (quicker)
"""
from __future__ import annotations

import os
import json
import argparse
from datetime import date, timedelta

from . import config, data, engine

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


def _print_probe(p: dict) -> bool:
    print("\n===== POLYGON TIER PROBE =====")
    for note in p["notes"]:
        print("  -", note)
    ok = p["aggregates_daily"]            # the one Phase 0 strictly requires
    print(f"  daily aggregates : {'OK' if p['aggregates_daily'] else 'MISSING'}")
    print(f"  all-tickers snap : {'OK' if p['snapshot_all'] else 'MISSING'}")
    print(f"  intraday minute  : {'OK' if p['intraday_minute'] else 'MISSING'}")
    if not p["aggregates_daily"]:
        print("  !! Daily aggregates unavailable -- backtest cannot run on this tier.")
    if not p["snapshot_all"]:
        print("  !! All-tickers snapshot missing -- needed later for live intraday runs.")
    if not p["intraday_minute"]:
        print("  (intraday minute missing -- backtest still works via daily H/L proxy)")
    print("==============================\n")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=config.DEFAULT_YEARS)
    ap.add_argument("--max-tickers", type=int, default=0, help="0 = full universe")
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--probe", action="store_true", help="probe tier and exit")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)

    # ---- probe ----
    probe = data.probe_tier()
    ok = _print_probe(probe)
    with open(os.path.join(RESULTS_DIR, "tier_probe.json"), "w") as f:
        json.dump(probe, f, indent=2)
    if args.probe:
        return
    if not ok:
        print("Aborting: Polygon tier does not cover daily aggregates.")
        return

    # ---- date window ----
    end = date.today()
    start = end - timedelta(days=int(args.years * 365) + config.MIN_HISTORY_BARS * 2)
    frm, to = start.isoformat(), end.isoformat()

    universe = list(config.WATCHLIST)
    if config.BENCHMARK not in universe:
        universe.append(config.BENCHMARK)
    if args.max_tickers:
        universe = universe[: args.max_tickers]
        if config.BENCHMARK not in universe:
            universe.append(config.BENCHMARK)

    print(f"Universe: {len(universe)} tickers | window {frm} -> {to}")
    bars = data.fetch_universe(universe, frm, to, use_cache=not args.no_cache)
    print(f"Fetched data for {len(bars)} tickers.")
    if config.BENCHMARK not in bars:
        print(f"  [WARN] no {config.BENCHMARK} data -- SPY comparison unavailable.")

    prep = engine.prepare(bars)

    # backtest window excludes the warm-up needed for the 50d MA
    cal = prep["calendar"]
    test_start = cal[config.MIN_HISTORY_BARS] if len(cal) > config.MIN_HISTORY_BARS else cal[0]
    test_end = cal[-1]

    # ---- walk-forward ----
    print("\nRunning walk-forward weight tuning...")
    wf = engine.walk_forward(prep, fast=args.fast)

    # ---- full-window backtest on recommended weights ----
    print("Running full-window backtest on recommended weights...")
    full_sim = engine.simulate(prep, wf["recommended_weights"], test_start, test_end)
    full_m = engine.metrics(full_sim, prep, test_start, test_end)

    # ---- intraday-exit benefit ----
    print("Running intraday-exit benefit test...")
    intra = engine.intraday_benefit(prep, wf["recommended_weights"], test_start, test_end)

    results = {
        "generated": date.today().isoformat(),
        "window": [test_start, test_end],
        "universe_size": len(bars),
        "recommended_weights": wf["recommended_weights"],
        "gate_pass": wf["gate_pass"],
        "walk_forward": wf,
        "full_window_metrics": full_m,
        "intraday_benefit": intra,
    }
    with open(os.path.join(RESULTS_DIR, "phase0_results.json"), "w") as f:
        json.dump(results, f, indent=2)

    # equity curve csv
    with open(os.path.join(RESULTS_DIR, "equity_curve.csv"), "w") as f:
        f.write("date,equity\n")
        for d, v in full_sim["equity"]:
            f.write(f"{d},{v:.2f}\n")

    _write_report(results)
    _print_summary(results)


def _write_report(r: dict):
    m = r["full_window_metrics"]; wf = r["walk_forward"]; agg = wf["oos_aggregate"]
    ib = r["intraday_benefit"]
    w = r["recommended_weights"]

    def pct(x):
        return "n/a" if x is None else f"{x*100:.2f}%"

    lines = [
        "# Phase 0 Backtest -- Technical Core",
        f"_Generated {r['generated']} | window {r['window'][0]} -> {r['window'][1]} "
        f"| {r['universe_size']} tickers_",
        "",
        f"## GATE: {'PASS' if r['gate_pass'] else 'FAIL'}",
        "Go-live requires positive out-of-sample expectancy that beats SPY "
        "net of a cost/slippage haircut.",
        "",
        "## Recommended weights",
        f"- trend: {w['trend']:.2f}  | momentum: {w['momentum']:.2f}  "
        f"| breakout: {w['breakout']:.2f}  | volume: {w['volume']:.2f}",
        "",
        "## Out-of-sample (walk-forward aggregate) -- the gate evidence",
        f"- trades: {agg['trades']}",
        f"- win rate: {pct(agg['win_rate'])}",
        f"- expectancy/trade: {pct(agg['expectancy'])}",
        f"- compounded OOS return: {pct(agg['compound_return'])}",
        f"- SPY over same OOS spans: {pct(agg['spy_return'])}",
        f"- **vs SPY: {pct(agg['vs_spy'])}**",
        "",
        "## Full-window backtest (recommended weights)",
        f"- trades: {m['trades']}",
        f"- win rate: {pct(m['win_rate'])}",
        f"- avg PnL/trade: {pct(m['avg_pnl_pct'])}",
        f"- total return: {pct(m['total_return'])}",
        f"- max drawdown: {pct(m['max_drawdown'])}",
        f"- Sharpe (annualised): {m['sharpe']}",
        f"- SPY buy & hold: {pct(m['spy_return'])}  | vs SPY: {pct(m['vs_spy'])}",
        "",
        "## Intraday-exit benefit (daily-close vs intraday-checked exits)",
        f"- daily-exit total return: {pct(ib['daily_exit']['total_return'])}",
        f"- intraday-exit total return: {pct(ib['intraday_exit']['total_return'])}",
        f"- **return improvement: {pct(ib['return_improvement'])}**",
        f"- expectancy improvement: {pct(ib['expectancy_improvement'])}",
        "",
        "## Per-fold detail",
    ]
    for fr in wf["folds"]:
        om = fr["oos_metrics"]
        lines.append(
            f"- Fold {fr['fold']}: IS {fr['is'][0]}->{fr['is'][1]}, "
            f"OOS {fr['oos'][0]}->{fr['oos'][1]} | weights "
            f"t{fr['weights']['trend']:.2f}/m{fr['weights']['momentum']:.2f}/"
            f"b{fr['weights']['breakout']:.2f}/v{fr['weights']['volume']:.2f} "
            f"| OOS trades {om['trades']}, win {pct(om['win_rate'])}, "
            f"vs SPY {pct(om['vs_spy'])}"
        )
    lines += ["", "_Paper-trading simulation. Not financial advice._"]
    with open(os.path.join(RESULTS_DIR, "phase0_report.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


def _print_summary(r: dict):
    m = r["full_window_metrics"]; agg = r["walk_forward"]["oos_aggregate"]
    print("\n" + "=" * 52)
    print(f"  PHASE 0 GATE: {'PASS' if r['gate_pass'] else 'FAIL'}")
    print("=" * 52)
    print(f"  OOS trades:        {agg['trades']}")
    print(f"  OOS expectancy:    {agg['expectancy']}")
    print(f"  OOS vs SPY:        {agg['vs_spy']}")
    print(f"  Full-window return:{m['total_return']}  (SPY {m['spy_return']})")
    print(f"  Max drawdown:      {m['max_drawdown']}   Sharpe {m['sharpe']}")
    print(f"  Recommended wts:   {r['recommended_weights']}")
    print("  Results -> backtest/results/ (report, json, equity csv)")
    print("=" * 52 + "\n")


if __name__ == "__main__":
    main()
