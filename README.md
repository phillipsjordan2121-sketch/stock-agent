# Tech/AI/Semiconductor Paper-Trading Agent

An automated **paper-trading** agent that runs every weekday on GitHub Actions. It pulls news and market data, asks Claude Haiku to analyze the watchlist, then opens, manages, and closes simulated positions in a **$100,000 paper portfolio**. No real money and no brokerage connection — all state lives in JSON files committed back to this repo after each run.

It runs at **11:45 AM UTC (6:45 AM Central), Monday–Friday**, and emails a full daily report.

## What it does each run

The pipeline runs three scripts in order:

1. **`position_checker.py`** — fetches live prices for open positions and applies price-based sell rules (stop-loss, hold-expiry, profit-lock, trailing stop, and an −8% portfolio circuit breaker).
2. **`learning_summary.py`** — reads closed-trade history and writes `learning_context.txt` (Bayesian, recency-weighted signal win-rates and confidence-band calibration). No API calls.
3. **`main.py`** — sweeps the watchlist with a **two-stage funnel** (see below), asks Claude for new picks and verdicts on open positions, scores each pick into a 0–100 confidence, opens the ones that clear the rules, closes any the model flags, logs every rejected pick, and emails the report.

## Two-stage research funnel

The watchlist is large (200 tickers), so `main.py` does **not** make the full set of API calls on every name. Instead:

- **Stage 1 — screen.** One cheap `/quote` call per ticker across all 200 (price + intraday % change). This is the momentum proxy and the screen.
- **Shortlist.** A ticker earns a deep dive if it's an abnormal mover (`|intraday %| ≥ SCREEN_DP_PCT`), is named in today's market news, or is an open position (always re-researched). Ranked held-first then by move size, capped at `MAX_DEEP_DIVE` (30). A floor of 10 guarantees research happens on quiet days.
- **Stage 2 — deep dive.** Company news, analyst recommendations, and price targets are fetched **only** for the shortlist.

This keeps the sweep at ~290 Finnhub calls (~5.5 min) regardless of universe size — adding tickers grows Stage 1 by one call each, not five. Diagnostics for both stages print at the start of each run.

## Key parameters (in `main.py`)

| Parameter | Value | Meaning |
|---|---|---|
| `CONFIDENCE_THRESHOLD` | 60 | Minimum score to open a position |
| `MAX_PICKS_PER_DAY` | None | No per-run cap — opens every pick that clears confidence until `EXPOSURE_CEILING` is hit |
| `EXPOSURE_CEILING` | 35% | Max share of total value invested at once |
| `MAX_SINGLE_POSITION_PCT` | 10% | Max share of total value in one position |
| `PORTFOLIO_STOP_PCT` | −8% | Circuit breaker: pause new picks if portfolio drops this far |
| `FINNHUB_SLEEP` | 1.1s | Spacing between Finnhub calls (free tier: 60/min) |
| `SCREEN_DP_PCT` | 2.5% | Stage-1 screen: `|intraday %|` ≥ this flags a ticker for deep research |
| `MAX_DEEP_DIVE` | 30 | Max tickers that get the full Stage-2 deep dive per run |

Position size scales with confidence: 90–100 → 8%, 80–89 → 6%, 70–79 → 4%, 60–69 → 2% of total value. Multiple positions in the same sector are allowed — picks are filtered on confidence, not diversification. Concentration is bounded only by `MAX_SINGLE_POSITION_PCT` (10%) and `EXPOSURE_CEILING` (35%).

## Watchlist (200 tickers)

Spanning 14 sectors: semiconductors (33), semi equipment (17), hardware (12), networking (16), cloud/AI mega-caps (6), software (37), cybersecurity (14), internet (16), fintech (16), EV/auto tech (10), IT services (8), quantum (4), media/streaming (6), and space/defense tech (5). Sectors are now used only for labelling and reporting — they no longer cap how many positions can be held at once. The full mapping is the `SECTOR_MAP` dict in `main.py`.

## State files

| File | Contents |
|---|---|
| `portfolio.json` | Live paper portfolio (cash, open positions, totals) |
| `trades.json` | All closed trades |
| `rejected.json` | Every pick blocked by the rules, with the reason |
| `learning_context.txt` | Generated each run; fed back into Claude's prompt |

## Setup

### 1. Fork this repo, then add five secrets

In your fork: **Settings → Secrets and variables → Actions → New repository secret**. The secret names must match exactly what the workflow references:

| Secret name | Value |
|---|---|
| `FINN_HUB_API_KEY` | Your Finnhub API key |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `GMAIL_USER` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | 16-char Gmail App Password (requires 2-Step Verification on) |
| `EMAIL_RECIPIENT` | Where to send the daily report |

If any of `GMAIL_USER`, `GMAIL_APP_PASSWORD`, or `EMAIL_RECIPIENT` is missing, the email step is silently skipped and the rest of the pipeline still runs.

### 2. Enable Actions, then test

Open the **Actions** tab and enable workflows if prompted, then **Actions → Daily Stock Picks → Run workflow** to trigger a run immediately and confirm the report email arrives.

## Customization

- **Confidence threshold / sizing / ceilings** — constants at the top of `main.py`.
- **Sell rules** — thresholds at the top of `position_checker.py` (`STOP_LOSS_PCT`, `PROFIT_LOCK_PCT`, `TRAILING_DROP_PCT`).
- **Watchlist** — the `SECTOR_MAP` dict in `main.py`.
- **Schedule** — the `cron` in `.github/workflows/daily.yml` (currently `45 11 * * 1-5`, in UTC).

## A note on Finnhub data

The agent uses Finnhub's free tier. Quotes and news for these US large-caps are well covered, but some endpoints (e.g. historical candles) may be gated to paid plans. To keep a 200-ticker sweep cheap, momentum is derived universe-wide from each `/quote`'s intraday percent-change rather than per-ticker candles, so no candle call is needed during the sweep. SPY trend and VIX still use candle/index quotes and fail gracefully to `unknown` if gated. The Stage-1/Stage-2 data-health block prints at the start of each run so you can see exactly what's flowing.

## Cost

GitHub Actions and Gmail are free; Finnhub runs on the free tier; Claude Haiku is roughly $0.01 per run (~$0.20/month).

---

*Paper-trading simulation for informational purposes only. Not financial advice.*
