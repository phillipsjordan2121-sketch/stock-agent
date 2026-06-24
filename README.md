# Tech/AI/Semiconductor Paper-Trading Agent

An automated **paper-trading** agent that runs every weekday on GitHub Actions. It pulls news and market data, asks Claude Haiku to analyze the watchlist, then opens, manages, and closes simulated positions in a **$100,000 paper portfolio**. No real money and no brokerage connection — all state lives in JSON files committed back to this repo after each run.

It runs at **11:45 AM UTC (6:45 AM Central), Monday–Friday**, and emails a full daily report.

## What it does each run

The pipeline runs three scripts in order:

1. **`position_checker.py`** — fetches live prices for open positions and applies price-based sell rules (stop-loss, hold-expiry, profit-lock, trailing stop, and an −8% portfolio circuit breaker).
2. **`learning_summary.py`** — reads closed-trade history and writes `learning_context.txt` (Bayesian, recency-weighted signal win-rates and confidence-band calibration). No API calls.
3. **`main.py`** — fetches news / price context / analyst data, asks Claude for new picks and verdicts on open positions, scores each pick into a 0–100 confidence, opens the ones that clear the rules, closes any the model flags, logs every rejected pick, and emails the report.

## Key parameters (in `main.py`)

| Parameter | Value | Meaning |
|---|---|---|
| `CONFIDENCE_THRESHOLD` | 60 | Minimum score to open a position |
| `MAX_PICKS_PER_DAY` | 3 | Max new positions per run |
| `EXPOSURE_CEILING` | 35% | Max share of total value invested at once |
| `MAX_SINGLE_POSITION_PCT` | 10% | Max share of total value in one position |
| `PORTFOLIO_STOP_PCT` | −8% | Circuit breaker: pause new picks if portfolio drops this far |
| `FINNHUB_SLEEP` | 1.1s | Spacing between Finnhub calls (free tier: 60/min) |

Position size scales with confidence: 90–100 → 8%, 80–89 → 6%, 70–79 → 4%, 60–69 → 2% of total value. Only one position per sector is held at a time.

## Watchlist (32 tickers)

Semiconductors (NVDA, AMD, TSM, AVGO, MRVL, QCOM, INTC, MU, ARM), semi equipment (AMAT, LRCX, KLAC, ASML), hardware (SMCI, DELL, HPE), cloud/AI (MSFT, GOOGL, AMZN, META, ORCL), software (CRM, SNOW, PLTR, NOW, DDOG, MDB, AI), cybersecurity (PANW, NET, CRWD), and quantum (IONQ).

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

The agent uses Finnhub's free tier. Quotes and news for these US large-caps are well covered, but some endpoints (e.g. historical candles) may be gated to paid plans. When the candle endpoint is unavailable, `main.py` falls back to an intraday percent-change proxy for the momentum signal and logs `Candle endpoint available: True/False` at the start of each run so you can see exactly what's flowing.

## Cost

GitHub Actions and Gmail are free; Finnhub runs on the free tier; Claude Haiku is roughly $0.01 per run (~$0.20/month).

---

*Paper-trading simulation for informational purposes only. Not financial advice.*
