# Tech/AI/Semiconductor Stock Agent

Runs every weekday at 5am PT. Pulls the last 24 hours of tech news and analyst actions, runs them through Claude, and emails you only the picks that clear 80% confidence.

## Setup (takes ~10 minutes)

### 1. Fork this repo
Click **Fork** at the top right of this page on GitHub.

### 2. Get your API keys

**Finnhub** (free)
- Go to [finnhub.io](https://finnhub.io) → Sign up → copy your API key

**Anthropic** (pay as you go, ~$0.01/day)
- Go to [console.anthropic.com](https://console.anthropic.com) → API Keys → Create key

**Gmail App Password** (free)
- Go to your Google Account → Security → 2-Step Verification (must be on) → App Passwords
- Create one named "Stock Agent" → copy the 16-character password

### 3. Add secrets to GitHub
In your forked repo: **Settings → Secrets and variables → Actions → New repository secret**

Add these 5 secrets:

| Secret name | Value |
|---|---|
| `FINNHUB_API_KEY` | Your Finnhub API key |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `GMAIL_USER` | Your Gmail address (e.g. you@gmail.com) |
| `GMAIL_APP_PASS` | The 16-char Gmail app password |
| `RECIPIENT_EMAIL` | Where to send the picks (can be same as GMAIL_USER) |

### 4. Enable Actions
Go to the **Actions** tab in your repo and click **"I understand my workflows, go ahead and enable them"** if prompted.

### 5. Test it manually
Go to **Actions → Daily Stock Picks → Run workflow** to trigger it immediately and confirm the email arrives.

## How it works

```
GitHub Actions (5am PT, Mon–Fri)
        ↓
Fetch last 24hrs from Finnhub
  · General tech news
  · Per-ticker news for 30+ watchlist stocks
  · Analyst buy/sell/hold counts
  · Consensus price targets
        ↓
Feed all data to Claude Haiku with the trading prompt
        ↓
Parse output — keep only picks ≥ 80% confidence
        ↓
If picks exist → send HTML email
If no picks clear threshold → no email (silence = no signal)
```

## Customization

**Change the confidence threshold** — edit `CONFIDENCE_THRESHOLD` in `main.py`

**Add/remove tickers** — edit the `WATCHLIST` list in `main.py`

**Change the schedule** — edit the cron in `.github/workflows/daily.yml`
- 5am PT (summer/PDT) = `0 12 * * 1-5`
- 5am PT (winter/PST) = `0 13 * * 1-5`

**Add a second sector agent** — duplicate this repo, swap the prompt and watchlist

## Cost estimate
- GitHub Actions: free (well within the 2,000 min/month free tier)
- Finnhub: free tier (60 API calls/min, plenty)
- Claude Haiku: ~$0.01 per run → ~$0.20/month
- Gmail: free

---

*This tool is for informational purposes only. Not financial advice.*
