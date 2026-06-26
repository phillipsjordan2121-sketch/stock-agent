# Phase 0 Backtest — Technical Core

Point-in-time backtest of the new **technical/momentum core** strategy on
Polygon daily history. This is the **HARD GATE** from the upgrade plan: the
strategy goes live only if walk-forward, out-of-sample expectancy is positive
and beats SPY net of a cost/slippage haircut.

## Isolation (why this can't break the live agent)

- Lives entirely under `backtest/`. Reads Polygon history; writes only to
  `backtest/results/` and `backtest/cache/`.
- **Never** touches `portfolio.json`, `trades.json`, `rejected.json`, or
  `learning_context.txt`.
- The workflow (`.github/workflows/phase0_backtest.yml`) is **manual-only**
  (`workflow_dispatch`, no cron), has **`contents: read`** permission (so it
  cannot commit anything), and only uploads results as an artifact.
- The live daily pipeline (`daily.yml`) is completely unchanged.

## What it does

1. **Probe** the Polygon tier (daily aggregates, all-tickers snapshot, intraday
   minute bars).
2. Pull ~1–2yr of daily OHLCV for the watchlist + SPY.
3. Compute the technical-core signals (trend, cross-sectional momentum,
   breakout, volume) with RSI/MACD guards; score linearly 0–100 (≥60 to enter).
4. Replay bar-by-bar with the live gating/sizing and the new exit set
   (stop −7%, profit-lock +10%, trailing −5%, momentum-break, 5-day hold,
   −8% circuit breaker).
5. **Walk-forward**: tune signal weights on in-sample windows, validate
   out-of-sample (guards against curve-fitting).
6. **Intraday-exit benefit**: compare daily-close exits vs intraday-checked
   exits (daily H/L proxy) to quantify slippage saved.
7. Metrics vs SPY; write report + JSON + equity curve; print a PASS/FAIL gate.

## Run it

**Via GitHub Actions (recommended):** Actions → "Phase 0 Backtest (manual)" →
Run workflow. Requires the `POLYGON_API_KEY` repo secret. Download the
`phase0-results` artifact when it finishes.

**Probe only** (confirm the key/tier without a full run): tick `probe_only`.

**Locally:**
```bash
pip install -r requirements-backtest.txt
export POLYGON_API_KEY=...        # your key, in the shell env only
python -m backtest.run_phase0 --years 2          # full
python -m backtest.run_phase0 --probe            # tier check only
python -m backtest.run_phase0 --years 2 --fast   # quicker, thinned grid
```

**Offline self-test** (no key, no network — validates the engine logic):
```bash
python -m backtest.selftest
```

## Outputs (`backtest/results/`)

- `phase0_report.md` — human-readable summary + the GATE verdict.
- `phase0_results.json` — full metrics, per-fold detail, recommended weights.
- `equity_curve.csv` — daily equity for the full-window run.
- `tier_probe.json` — what the Polygon key can access.

## Key assumptions / honest caveats

- Entries fill at the signal bar's close; exits in intraday mode fill at the
  rule's threshold price using the bar's H/L as the intraday-range proxy (true
  minute bars would refine the intraday-benefit estimate — a later hook).
- A round-trip cost/slippage haircut (`COST_ROUNDTRIP_PCT`, default 20 bps) is
  applied to every trade; the gate is net of it.
- The circuit breaker pauses new entries while total return ≤ −8% and
  auto-resumes on recovery (a backtest interpretation of the live pause flag).
- No spreads/commissions beyond the haircut; momentum underperforms in chop —
  the exit rules + circuit breaker are the safety net.

_Paper-trading simulation. Not financial advice._
