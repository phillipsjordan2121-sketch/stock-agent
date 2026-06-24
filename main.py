"""
main.py
-------
Core paper-trading agent. Runs as step 3 of the daily pipeline, AFTER
position_checker.py (price-based sell rules) and learning_summary.py.

Pipeline role:
  1. Fetch news + price-context + analyst data from Finnhub (rate-limited).
  2. Build one prompt (open positions + learning context + today's data) and ask
     Claude Haiku for new_picks + position_verdicts (JSON only).
  3. Score each new pick from its signals/deductions -> confidence (0-100).
  4. Apply gating rules and open accepted picks into portfolio.json.
  5. Apply Claude's exit verdicts (rules 6-7) and close positions to trades.json.
  6. Log every blocked pick to rejected.json.
  7. Email a full daily report.
"""

import os
import re
import json
import time
import smtplib
import requests
from datetime import datetime, timedelta, timezone, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import anthropic

# -- Secrets / env -------------------------------------------------------------
FINNHUB_API_KEY   = os.environ.get("FINNHUB_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GMAIL_USER        = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASS    = os.environ.get("GMAIL_APP_PASS", "")
RECIPIENT_EMAIL   = os.environ.get("RECIPIENT_EMAIL", GMAIL_USER)

# -- Tunable parameters --------------------------------------------------------
CLAUDE_MODEL            = "claude-haiku-4-5-20251001"
FINNHUB_SLEEP           = 1.1
MAX_PICKS_PER_DAY       = 3
CONFIDENCE_THRESHOLD    = 60
EXPOSURE_CEILING        = 0.35
MAX_SINGLE_POSITION_PCT = 0.10
PORTFOLIO_STOP_PCT      = -0.08
MOMENTUM_DP_PCT         = 3.0     # free-tier proxy: intraday % change >= 3% counts as momentum (when candles gated)

PORTFOLIO_FILE = "portfolio.json"
TRADES_FILE    = "trades.json"
REJECTED_FILE  = "rejected.json"
LEARNING_FILE  = "learning_context.txt"

# -- Watchlist + sectors -------------------------------------------------------
SECTOR_MAP = {
    "NVDA": "semiconductor", "AMD": "semiconductor", "TSM": "semiconductor",
    "AVGO": "semiconductor", "MRVL": "semiconductor", "QCOM": "semiconductor",
    "INTC": "semiconductor", "MU": "semiconductor", "ARM": "semiconductor",
    "AMAT": "semi_equipment", "LRCX": "semi_equipment", "KLAC": "semi_equipment",
    "ASML": "semi_equipment",
    "SMCI": "hardware", "DELL": "hardware", "HPE": "hardware",
    "MSFT": "cloud_ai", "GOOGL": "cloud_ai", "AMZN": "cloud_ai",
    "META": "cloud_ai", "ORCL": "cloud_ai",
    "CRM": "software", "SNOW": "software", "PLTR": "software", "NOW": "software",
    "DDOG": "software", "MDB": "software", "AI": "software",
    "PANW": "cybersecurity", "NET": "cybersecurity", "CRWD": "cybersecurity",
    "IONQ": "quantum",
}
WATCHLIST = list(SECTOR_MAP.keys())

COMPANY_NAMES = {
    "NVDA": "NVIDIA Corporation", "AMD": "Advanced Micro Devices",
    "TSM": "Taiwan Semiconductor", "AVGO": "Broadcom", "MRVL": "Marvell Technology",
    "QCOM": "Qualcomm", "INTC": "Intel", "MU": "Micron Technology", "ARM": "Arm Holdings",
    "AMAT": "Applied Materials", "LRCX": "Lam Research", "KLAC": "KLA Corporation",
    "ASML": "ASML Holding", "SMCI": "Super Micro Computer", "DELL": "Dell Technologies",
    "HPE": "Hewlett Packard Enterprise", "MSFT": "Microsoft", "GOOGL": "Alphabet",
    "AMZN": "Amazon", "META": "Meta Platforms", "ORCL": "Oracle", "CRM": "Salesforce",
    "SNOW": "Snowflake", "PLTR": "Palantir", "NOW": "ServiceNow", "DDOG": "Datadog",
    "MDB": "MongoDB", "AI": "C3.ai", "PANW": "Palo Alto Networks", "NET": "Cloudflare",
    "CRWD": "CrowdStrike", "IONQ": "IonQ",
}

IGNORED_KEYWORDS = ["options alert", "shareholder lawsuit", "technical analysis", "form 4"]

# -- Signal scoring ------------------------------------------------------------
SIGNAL_POINTS = {
    "analyst_upgrade_strong":     25,
    "earnings_beat":              25,
    "guidance_raise":             20,
    "analyst_upgrade":            20,
    "price_target_raised_major":  20,
    "price_target_raised":        15,
    "earnings_beat_minor":        15,
    "company_specific_news":      10,
    "momentum":                   10,
    "secondary_beneficiary":      10,
    "sector_tailwind":             5,
}
DEDUCTION_POINTS = {
    "sector_tailwind_only": -30,
    "mixed_signals":        -15,
    "correlated_pick":      -15,
    "high_vix":             -10,
    "late_week_entry":       -5,
}
CONFIDENCE_ALLOC = [
    (90, 100, 0.08),
    (80,  89, 0.06),
    (70,  79, 0.04),
    (60,  69, 0.02),
]


# -- I/O helpers ---------------------------------------------------------------

def load_json(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def read_text(path, default=""):
    try:
        with open(path, "r") as f:
            return f.read()
    except FileNotFoundError:
        return default


# =============================================================================
# Finnhub data fetching (all rate-limited)
# =============================================================================

def _get(url, params, timeout=12):
    params = dict(params, token=FINNHUB_API_KEY)
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_quote(ticker):
    """Current price ('c'); fall back to previous close ('pc'). None on failure."""
    try:
        data = _get("https://finnhub.io/api/v1/quote", {"symbol": ticker})
        cur = data.get("c", 0.0)
        return float(cur) if cur else (float(data.get("pc", 0.0)) or None)
    except Exception as e:
        print(f"  [WARN] fetch_quote({ticker}) failed: {e}")
        return None


def fetch_quote_data(ticker):
    """One /quote call -> {'price': float|None, 'dp': float|None}. dp = intraday % change."""
    try:
        data = _get("https://finnhub.io/api/v1/quote", {"symbol": ticker})
        cur = data.get("c", 0.0)
        price = float(cur) if cur else (float(data.get("pc", 0.0)) or None)
        dp = data.get("dp")
        dp = float(dp) if dp is not None else None
        return {"price": price, "dp": dp}
    except Exception as e:
        print(f"  [WARN] fetch_quote_data({ticker}) failed: {e}")
        return None


def candle_available():
    """Probe whether Finnhub's /stock/candle endpoint is accessible on this tier."""
    try:
        now = int(time.time())
        data = _get("https://finnhub.io/api/v1/stock/candle",
                    {"symbol": "AAPL", "resolution": "D", "from": now - 7 * 86400, "to": now})
        return data.get("s") == "ok"
    except Exception:
        return False


def fetch_market_news(lookback_hours, max_articles):
    """General tech news, filtered, deduped, chronologically sorted."""
    try:
        articles = _get("https://finnhub.io/api/v1/news", {"category": "technology"})
    except Exception as e:
        print(f"  [WARN] fetch_market_news failed: {e}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    seen, kept = set(), []
    for a in sorted(articles, key=lambda x: x.get("datetime", 0)):
        ts = a.get("datetime", 0)
        if datetime.fromtimestamp(ts, tz=timezone.utc) < cutoff:
            continue
        head = (a.get("headline") or "").strip()
        if not head or head.lower() in seen:
            continue
        if any(k in head.lower() for k in IGNORED_KEYWORDS):
            continue
        seen.add(head.lower())
        when = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")
        kept.append(f"[{when}] {head} - {(a.get('summary') or '')[:200]}")
    return kept[:max_articles]


def fetch_company_news(ticker, lookback_hours):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    frm = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d")
    try:
        articles = _get("https://finnhub.io/api/v1/company-news",
                        {"symbol": ticker, "from": frm, "to": today})
        lines = []
        for a in articles[:3]:
            head = (a.get("headline") or "").strip()
            if head and not any(k in head.lower() for k in IGNORED_KEYWORDS):
                lines.append(f"[{ticker}] {head} - {(a.get('summary') or '')[:150]}")
        return lines
    except Exception:
        return []


def fetch_momentum(ticker):
    """5-day window; close-over-close >= 3% => True. Premium-safe (returns None)."""
    try:
        now = int(time.time())
        data = _get("https://finnhub.io/api/v1/stock/candle",
                    {"symbol": ticker, "resolution": "D", "from": now - 7 * 86400, "to": now})
        if data.get("s") != "ok" or len(data.get("c", [])) < 2:
            return None
        closes = data["c"]
        change = (closes[-1] - closes[0]) / closes[0] if closes[0] else 0
        return change >= 0.03
    except Exception:
        return None


def fetch_spy_trend():
    """14-day SPY trend: up / down / sideways. 'unknown' if unavailable."""
    try:
        now = int(time.time())
        data = _get("https://finnhub.io/api/v1/stock/candle",
                    {"symbol": "SPY", "resolution": "D", "from": now - 20 * 86400, "to": now})
        if data.get("s") != "ok" or len(data.get("c", [])) < 2:
            return "unknown"
        closes = data["c"]
        change = (closes[-1] - closes[0]) / closes[0] if closes[0] else 0
        if change >= 0.01:
            return "up"
        if change <= -0.01:
            return "down"
        return "sideways"
    except Exception:
        return "unknown"


def fetch_vix():
    """VIX level. None if unavailable (index quotes are often premium-gated)."""
    try:
        data = _get("https://finnhub.io/api/v1/quote", {"symbol": "^VIX"})
        v = data.get("c", 0.0)
        return float(v) if v else None
    except Exception:
        return None


def fetch_analyst(ticker):
    try:
        data = _get("https://finnhub.io/api/v1/stock/recommendation", {"symbol": ticker})
        if not data:
            return None
        d = data[0]
        return (f"{ticker}: period {d.get('period','?')} | StrongBuy={d.get('strongBuy',0)} "
                f"Buy={d.get('buy',0)} Hold={d.get('hold',0)} Sell={d.get('sell',0)} "
                f"StrongSell={d.get('strongSell',0)}")
    except Exception:
        return None


def fetch_price_target(ticker, current_price):
    try:
        d = _get("https://finnhub.io/api/v1/stock/price-target", {"symbol": ticker})
        mean = d.get("targetMean")
        if not mean:
            return None
        vs = ""
        if current_price:
            upside = (mean - current_price) / current_price
            vs = f" | current=${current_price:.2f} ({upside:+.1%} to mean)"
        return (f"{ticker}: mean=${mean:.2f} high=${d.get('targetHigh',0):.2f} "
                f"low=${d.get('targetLow',0):.2f}{vs}")
    except Exception:
        return None


def gather_market_data():
    """One rate-limited sweep. Returns context blocks + a price map."""
    is_monday = datetime.now(timezone.utc).weekday() == 0
    lookback = 72 if is_monday else 24
    max_articles = 60 if is_monday else 40

    print(f"Lookback {lookback}h, up to {max_articles} market articles.")
    market_news = fetch_market_news(lookback, max_articles)
    time.sleep(FINNHUB_SLEEP)

    spy_trend = fetch_spy_trend()
    time.sleep(FINNHUB_SLEEP)
    vix = fetch_vix()
    time.sleep(FINNHUB_SLEEP)

    # Probe the candle endpoint once. If it's gated on this Finnhub tier, derive
    # momentum from each /quote's intraday % change (dp) instead of 5-day candles.
    candle_ok = candle_available()
    time.sleep(FINNHUB_SLEEP)
    print(f"Candle endpoint available: {candle_ok} "
          f"(momentum via {'5-day candles' if candle_ok else 'intraday %-change proxy'})")

    company_news, momentum, analyst, targets, prices = [], {}, [], [], {}

    print(f"Sweeping {len(WATCHLIST)} tickers (rate-limited)...")
    for t in WATCHLIST:
        qd = fetch_quote_data(t); time.sleep(FINNHUB_SLEEP)
        price = qd["price"] if qd else None
        prices[t] = price

        company_news += fetch_company_news(t, lookback); time.sleep(FINNHUB_SLEEP)

        if candle_ok:
            momentum[t] = fetch_momentum(t); time.sleep(FINNHUB_SLEEP)
        else:
            dp = qd.get("dp") if qd else None
            momentum[t] = (dp >= MOMENTUM_DP_PCT) if dp is not None else None

        a = fetch_analyst(t); time.sleep(FINNHUB_SLEEP)
        if a:
            analyst.append(a)

        pt = fetch_price_target(t, price); time.sleep(FINNHUB_SLEEP)
        if pt:
            targets.append(pt)

    mom_lines = []
    for t in WATCHLIST:
        m = momentum[t]
        label = "confirmed" if m is True else ("no" if m is False else "unknown")
        mom_lines.append(f"{t}={label}")

    return {
        "market_news": market_news,
        "company_news": company_news,
        "momentum": momentum,
        "momentum_block": " ".join(mom_lines),
        "spy_trend": spy_trend,
        "vix": vix,
        "analyst": analyst,
        "targets": targets,
        "prices": prices,
    }


# =============================================================================
# Prompt construction (.replace, NOT .format -- prompt contains JSON braces)
# =============================================================================

PROMPT_TEMPLATE = (
    "You are a short-term trading analyst covering Tech, AI, and Semiconductors. "
    "Using ONLY the data below, identify short-term (1-5 day hold) buy opportunities "
    "and judge existing open positions.\n\n"
    "Return JSON ONLY. No prose, no markdown fences. Exactly this schema:\n"
    "{\n"
    '  "new_picks": [\n'
    "    {\n"
    '      "ticker": "NVDA",\n'
    '      "company": "NVIDIA Corporation",\n'
    '      "signals": ["earnings_beat", "analyst_upgrade"],\n'
    '      "deductions": [],\n'
    '      "catalyst": "one sentence citing the specific news",\n'
    '      "invalidation": "what would signal early exit within the hold window",\n'
    '      "hold_days": 5\n'
    "    }\n"
    "  ],\n"
    '  "position_verdicts": [\n'
    '    { "ticker": "AMD", "action": "hold", "reason": null },\n'
    '    { "ticker": "MSFT", "action": "exit", "reason": "catalyst_reversal: why" }\n'
    "  ]\n"
    "}\n\n"
    "Rules:\n"
    "- Each new pick must be anchored to a SPECIFIC catalyst in NEWS DATA or ANALYST "
    "DATA. No generic sector tailwinds alone.\n"
    "- Choose signals ONLY from: analyst_upgrade_strong, earnings_beat, guidance_raise, "
    "analyst_upgrade, price_target_raised_major, price_target_raised, earnings_beat_minor, "
    "company_specific_news, momentum, secondary_beneficiary, sector_tailwind.\n"
    "- Choose deductions ONLY from: sector_tailwind_only, mixed_signals, correlated_pick, "
    "high_vix, late_week_entry.\n"
    "- Only assign the 'momentum' signal if PRICE CONTEXT shows that ticker's momentum is "
    "'confirmed'.\n"
    "- hold_days must be between 1 and 5.\n"
    "- For every OPEN POSITION, output a verdict: action 'hold' or 'exit'. Use 'exit' only "
    "on catalyst_reversal (thesis invalidated by new news) or confidence_decay (signal "
    "clearly weakened). Prefix the reason with 'catalyst_reversal:' or 'confidence_decay:'.\n"
    "- Do not output confidence numbers; the scoring layer computes those from your signals.\n\n"
    "TODAY: __TODAY__\n\n"
    "PRICE CONTEXT (momentum per ticker, SPY trend, VIX):\n"
    "SPY trend: __SPY__ | VIX: __VIX__\n"
    "Momentum: __MOMENTUM__\n\n"
    "NEWS DATA:\n__NEWS__\n\n"
    "ANALYST DATA:\n__ANALYST__\n\n"
    "PRICE TARGETS:\n__TARGETS__\n\n"
    "OPEN POSITIONS:\n__OPEN_POSITIONS__\n\n"
    "LEARNING CONTEXT:\n__LEARNING__\n"
)


def build_prompt(data, portfolio, learning_context):
    today = datetime.now(timezone.utc)
    today_str = f"{today.strftime('%Y-%m-%d')} ({today.strftime('%A')})"

    news = "\n".join(data["market_news"] + data["company_news"]) or "(no fresh news)"
    analyst = "\n".join(data["analyst"]) or "(none)"
    targets = "\n".join(data["targets"]) or "(none)"
    vix = f"{data['vix']:.1f}" if data["vix"] is not None else "unknown"

    positions = portfolio.get("positions", {})
    if positions:
        op_lines = []
        for t, p in positions.items():
            op_lines.append(
                f"{t} ({p.get('sector','?')}): entry {p.get('entry_date','?')} @ "
                f"${p.get('entry_price',0):.2f}, conf {p.get('confidence_at_entry','?')}, "
                f"signals {p.get('signals',[])}, catalyst: {p.get('catalyst','')}, "
                f"invalidation: {p.get('invalidation','')}"
            )
        open_positions = "\n".join(op_lines)
    else:
        open_positions = "(none open)"

    out = PROMPT_TEMPLATE
    out = out.replace("__TODAY__", today_str)
    out = out.replace("__SPY__", data["spy_trend"])
    out = out.replace("__VIX__", vix)
    out = out.replace("__MOMENTUM__", data["momentum_block"])
    out = out.replace("__NEWS__", news)
    out = out.replace("__ANALYST__", analyst)
    out = out.replace("__TARGETS__", targets)
    out = out.replace("__OPEN_POSITIONS__", open_positions)
    out = out.replace("__LEARNING__", learning_context or "(none)")
    return out


def run_analysis(prompt):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def parse_claude_json(raw):
    """Extract the JSON object, tolerating stray prose/fences."""
    txt = raw.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt)
        txt = re.sub(r"\n?```$", "", txt).strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    print("  [WARN] Could not parse Claude JSON. Raw:\n", raw[:1000])
    return {"new_picks": [], "position_verdicts": []}


# =============================================================================
# Scoring
# =============================================================================

def score_pick(pick, momentum_map):
    """Confidence (0-100) from signals/deductions, applying the momentum guard."""
    signals = [s for s in pick.get("signals", []) if s in SIGNAL_POINTS]
    deductions = [d for d in pick.get("deductions", []) if d in DEDUCTION_POINTS]
    ticker = pick.get("ticker", "")

    # Momentum guard: keep 'momentum' only if PRICE CONTEXT confirmed it.
    if "momentum" in signals and momentum_map.get(ticker) is not True:
        signals = [s for s in signals if s != "momentum"]

    score = sum(SIGNAL_POINTS[s] for s in signals)
    score += sum(DEDUCTION_POINTS[d] for d in deductions)
    score = max(0, min(100, score))
    return score, signals, deductions


def alloc_fraction(confidence):
    for lo, hi, frac in CONFIDENCE_ALLOC:
        if lo <= confidence <= hi:
            return frac
    return 0.0


# =============================================================================
# Position opening / closing
# =============================================================================

def close_position(portfolio, trades, ticker, price, reason, today_str):
    pos = portfolio["positions"].pop(ticker)
    shares = float(pos["shares"]); entry = float(pos["entry_price"])
    cost = shares * entry; exit_value = shares * price
    pnl = exit_value - cost
    pnl_pct = pnl / cost if cost > 0 else 0.0
    portfolio["cash"] = round(portfolio["cash"] + exit_value, 2)
    trades.append({
        "ticker": ticker, "company": pos.get("company", ticker),
        "sector": pos.get("sector", "unknown"),
        "entry_date": pos.get("entry_date", ""), "exit_date": today_str,
        "entry_price": entry, "exit_price": round(price, 4),
        "shares": round(shares, 6), "cost_basis": round(cost, 2),
        "exit_value": round(exit_value, 2), "pnl_dollar": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 6),
        "hold_days_actual": (date.fromisoformat(today_str) - date.fromisoformat(pos["entry_date"])).days
        if pos.get("entry_date") else 0,
        "hold_days_suggested": pos.get("hold_days", 3),
        "signals": pos.get("signals", []),
        "confidence_at_entry": pos.get("confidence_at_entry"),
        "exit_reason": reason, "spy_trend_at_entry": pos.get("spy_trend_at_entry", "unknown"),
        "post_exit_price": None, "post_exit_date": None,
        "catalyst": pos.get("catalyst", ""), "invalidation": pos.get("invalidation", ""),
    })
    portfolio["total_closed_trades"] = portfolio.get("total_closed_trades", 0) + 1
    print(f"  [EXIT] {ticker}: {reason} | pnl={pnl_pct:.2%}")


def recompute_totals(portfolio, prices):
    positions = portfolio.get("positions", {})
    invested = 0.0
    for t, p in positions.items():
        px = prices.get(t) or float(p.get("entry_price", 0))
        invested += float(p["shares"]) * px
    cash = float(portfolio.get("cash", 0.0))
    start = float(portfolio.get("starting_capital", 100000.0))
    total = cash + invested
    portfolio["total_invested"] = round(invested, 2)
    portfolio["portfolio_value"] = round(invested, 2)
    portfolio["total_value"] = round(total, 2)
    portfolio["total_pnl_pct"] = round((total - start) / start, 6)
    portfolio["open_position_count"] = len(positions)


def _reject(pick, today_str, reason, confidence, signals=None, deductions=None):
    return {
        "ticker": pick.get("ticker", "?"),
        "date": today_str,
        "rejection_reason": reason,
        "confidence": confidence,
        "signals": signals if signals is not None else pick.get("signals", []),
        "deductions": deductions if deductions is not None else pick.get("deductions", []),
    }


def process_picks(parsed, data, portfolio, trades, rejected, today_str):
    prices = data["prices"]
    momentum = data["momentum"]
    spy_trend = data["spy_trend"]
    paused = portfolio.get("paused", False)

    # -- 1. Exit verdicts (rules 6-7) --
    for v in (parsed.get("position_verdicts", []) or []):
        t = v.get("ticker")
        if v.get("action") == "exit" and t in portfolio.get("positions", {}):
            price = prices.get(t) or float(portfolio["positions"][t].get("entry_price", 0))
            close_position(portfolio, trades, t, price, v.get("reason") or "claude_exit", today_str)

    recompute_totals(portfolio, prices)

    # -- 2. New picks --
    new_opened = []
    if paused:
        for p in (parsed.get("new_picks", []) or []):
            rejected.append(_reject(p, today_str, "circuit_breaker_paused", None))
        print("  Portfolio paused (circuit breaker) - no new picks opened.")
        return new_opened

    scored = []
    for p in (parsed.get("new_picks", []) or []):
        conf, sigs, deds = score_pick(p, momentum)
        scored.append((conf, sigs, deds, p))
    scored.sort(key=lambda x: x[0], reverse=True)

    opened_today = 0
    for conf, sigs, deds, p in scored:
        t = p.get("ticker", "")
        sector = SECTOR_MAP.get(t, "unknown")

        if t not in SECTOR_MAP:
            rejected.append(_reject(p, today_str, "not_in_watchlist", conf, sigs, deds)); continue
        if opened_today >= MAX_PICKS_PER_DAY:
            rejected.append(_reject(p, today_str, "max_picks_reached", conf, sigs, deds)); continue
        if conf < CONFIDENCE_THRESHOLD:
            rejected.append(_reject(p, today_str, f"confidence_below_threshold: score={conf} < {CONFIDENCE_THRESHOLD}", conf, sigs, deds)); continue
        if t in portfolio.get("positions", {}):
            rejected.append(_reject(p, today_str, "already_held", conf, sigs, deds)); continue
        if any(pos.get("sector") == sector for pos in portfolio.get("positions", {}).values()):
            rejected.append(_reject(p, today_str, "sector_already_held", conf, sigs, deds)); continue

        price = prices.get(t)
        if not price:
            rejected.append(_reject(p, today_str, "no_price_available", conf, sigs, deds)); continue

        total_value = float(portfolio.get("total_value", 0.0))
        frac = min(alloc_fraction(conf), MAX_SINGLE_POSITION_PCT)
        target_dollars = total_value * frac

        invested = float(portfolio.get("total_invested", 0.0))
        if invested + target_dollars > EXPOSURE_CEILING * total_value:
            rejected.append(_reject(p, today_str, "exposure_ceiling", conf, sigs, deds)); continue

        if target_dollars > float(portfolio.get("cash", 0.0)):
            target_dollars = float(portfolio.get("cash", 0.0))
        if target_dollars < 1:
            rejected.append(_reject(p, today_str, "insufficient_cash", conf, sigs, deds)); continue

        shares = target_dollars / price
        cost = shares * price
        portfolio["cash"] = round(portfolio["cash"] - cost, 2)
        hd = p.get("hold_days", 3)
        portfolio.setdefault("positions", {})[t] = {
            "company": p.get("company", COMPANY_NAMES.get(t, t)),
            "sector": sector,
            "entry_date": today_str,
            "entry_price": round(price, 4),
            "shares": round(shares, 6),
            "cost_basis": round(cost, 2),
            "confidence_at_entry": conf,
            "signals": sigs,
            "deductions": deds,
            "catalyst": p.get("catalyst", ""),
            "invalidation": p.get("invalidation", ""),
            "hold_days": int(hd) if str(hd).isdigit() else 3,
            "spy_trend_at_entry": spy_trend,
            "peak_price": round(price, 4),
        }
        opened_today += 1
        new_opened.append((t, conf, cost, p))
        recompute_totals(portfolio, prices)
        print(f"  [OPEN] {t}: conf={conf} ${cost:.0f} ({shares:.4f} sh @ ${price:.2f})")

    return new_opened


# =============================================================================
# Email
# =============================================================================

def build_email(portfolio, new_opened, exits, rejected_today, data):
    today = datetime.now().strftime("%A, %B %d, %Y")
    vix = f"{data['vix']:.1f}" if data["vix"] is not None else "n/a"
    pv = portfolio
    paused = portfolio.get("paused", False)

    def card(t, p, conf):
        return (
            "<div style='background:#fff;border:1px solid #e5e7eb;border-radius:12px;"
            "padding:16px 20px;margin-bottom:12px;'>"
            f"<div style='font-size:18px;font-weight:800;color:#111;'>{t} "
            f"<span style='font-size:13px;color:#6b7280;font-weight:500;'>{p.get('company','')}</span>"
            f"<span style='float:right;background:#16a34a15;color:#16a34a;font-size:12px;"
            f"font-weight:700;border-radius:20px;padding:2px 10px;'>conf {conf}</span></div>"
            "<div style='font-size:13px;color:#374151;margin-top:8px;'>"
            f"<b>Catalyst:</b> {p.get('catalyst','')}<br>"
            f"<b>Signals:</b> {', '.join(p.get('signals', []))}<br>"
            f"<b>Hold:</b> {p.get('hold_days','?')}d &nbsp; "
            f"<b>Exit if:</b> <span style='color:#dc2626;'>{p.get('invalidation','')}</span>"
            "</div></div>"
        )

    new_html = "".join(card(t, p, conf) for t, conf, cost, p in new_opened) or \
        "<div style='color:#9ca3af;font-size:13px;'>No new positions opened today.</div>"

    exit_html = "".join(
        f"<div style='font-size:13px;color:#374151;margin-bottom:6px;'>"
        f"<b>{tr['ticker']}</b> exited - {tr['exit_reason']} (pnl {tr['pnl_pct']:.2%})</div>"
        for tr in exits
    ) or "<div style='color:#9ca3af;font-size:13px;'>No exits today.</div>"

    pos_html = "".join(
        f"<tr><td style='padding:4px 8px;font-weight:700;'>{t}</td>"
        f"<td style='padding:4px 8px;'>${p['entry_price']:.2f}</td>"
        f"<td style='padding:4px 8px;'>{p.get('confidence_at_entry','?')}</td>"
        f"<td style='padding:4px 8px;'>{p.get('hold_days','?')}d</td></tr>"
        for t, p in portfolio.get("positions", {}).items()
    ) or "<tr><td colspan='4' style='padding:4px 8px;color:#9ca3af;'>No open positions.</td></tr>"

    rej_html = "".join(
        f"<div style='font-size:12px;color:#6b7280;'>{r['ticker']}: {r['rejection_reason']}</div>"
        for r in rejected_today[:5]
    ) or "<div style='font-size:12px;color:#9ca3af;'>None.</div>"

    pnl = pv.get("total_pnl_pct", 0.0)
    pnl_color = "#16a34a" if pnl >= 0 else "#dc2626"
    paused_tag = " &#9888; PAUSED" if paused else ""

    return (
        "<html><body style=\"margin:0;background:#f9fafb;font-family:-apple-system,"
        "BlinkMacSystemFont,'Segoe UI',sans-serif;\">"
        "<div style='max-width:620px;margin:0 auto;padding:28px 16px;'>"
        "<div style='font-size:11px;font-weight:700;text-transform:uppercase;"
        "letter-spacing:.1em;color:#6b7280;'>Paper Portfolio - Tech/AI/Semi</div>"
        f"<div style='font-size:24px;font-weight:800;color:#111;'>Daily Report{paused_tag}</div>"
        f"<div style='font-size:13px;color:#9ca3af;margin-bottom:18px;'>{today} - "
        f"SPY {data['spy_trend']} - VIX {vix}</div>"
        "<div style='display:flex;gap:10px;margin-bottom:22px;flex-wrap:wrap;'>"
        "<div style='flex:1;background:#fff;border:1px solid #e5e7eb;border-radius:10px;"
        "padding:12px;min-width:120px;'><div style='font-size:11px;color:#9ca3af;'>Total Value</div>"
        f"<div style='font-size:20px;font-weight:800;color:#111;'>${pv.get('total_value',0):,.0f}</div></div>"
        "<div style='flex:1;background:#fff;border:1px solid #e5e7eb;border-radius:10px;"
        "padding:12px;min-width:120px;'><div style='font-size:11px;color:#9ca3af;'>Cash</div>"
        f"<div style='font-size:20px;font-weight:800;color:#111;'>${pv.get('cash',0):,.0f}</div></div>"
        "<div style='flex:1;background:#fff;border:1px solid #e5e7eb;border-radius:10px;"
        "padding:12px;min-width:120px;'><div style='font-size:11px;color:#9ca3af;'>Total P&L</div>"
        f"<div style='font-size:20px;font-weight:800;color:{pnl_color};'>{pnl:+.2%}</div></div>"
        "</div>"
        f"<h3 style='font-size:14px;color:#111;'>New Positions</h3>{new_html}"
        f"<h3 style='font-size:14px;color:#111;margin-top:20px;'>Exits</h3>{exit_html}"
        "<h3 style='font-size:14px;color:#111;margin-top:20px;'>Open Positions</h3>"
        "<table style='width:100%;border-collapse:collapse;font-size:13px;background:#fff;"
        "border:1px solid #e5e7eb;border-radius:8px;'>"
        "<tr style='color:#9ca3af;text-align:left;'><th style='padding:4px 8px;'>Ticker</th>"
        "<th style='padding:4px 8px;'>Entry</th><th style='padding:4px 8px;'>Conf</th>"
        f"<th style='padding:4px 8px;'>Hold</th></tr>{pos_html}</table>"
        f"<h3 style='font-size:14px;color:#111;margin-top:20px;'>Recent Rejections</h3>{rej_html}"
        "<div style='font-size:11px;color:#d1d5db;margin-top:22px;line-height:1.6;'>"
        "Automated paper-trading simulation. Not financial advice.</div>"
        "</div></body></html>"
    )


def send_email(portfolio, new_opened, exits, rejected_today, data):
    if not (GMAIL_USER and GMAIL_APP_PASS and RECIPIENT_EMAIL):
        print("Email secrets missing - skipping email.")
        return
    paused = " (PAUSED)" if portfolio.get("paused", False) else ""
    subject = (f"[Stock Agent] {datetime.now().strftime('%Y-%m-%d')} - "
               f"{len(new_opened)} new pick(s), {len(exits)} exit(s){paused}")
    html = build_email(portfolio, new_opened, exits, rejected_today, data)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject; msg["From"] = GMAIL_USER; msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASS)
            server.sendmail(GMAIL_USER, RECIPIENT_EMAIL, msg.as_string())
        print(f"Email sent: {subject}")
    except Exception as e:
        print(f"  [WARN] send_email failed: {e}")


# =============================================================================
# Main
# =============================================================================

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] main.py starting...")
    if not FINNHUB_API_KEY or not ANTHROPIC_API_KEY:
        print("Missing FINNHUB_API_KEY or ANTHROPIC_API_KEY - aborting.")
        return

    today_str = date.today().isoformat()
    portfolio = load_json(PORTFOLIO_FILE, {})
    trades    = load_json(TRADES_FILE, [])
    rejected  = load_json(REJECTED_FILE, [])
    learning  = read_text(LEARNING_FILE, "")

    if not portfolio:
        print("portfolio.json missing/empty - aborting.")
        return

    data = gather_market_data()
    print(f"News: {len(data['market_news'])} market + {len(data['company_news'])} company. "
          f"SPY={data['spy_trend']} VIX={data['vix']}")

    prompt = build_prompt(data, portfolio, learning)
    print("Calling Claude...")
    raw = run_analysis(prompt)
    parsed = parse_claude_json(raw)
    print(f"Claude: {len(parsed.get('new_picks', []))} candidate pick(s), "
          f"{len(parsed.get('position_verdicts', []))} verdict(s).")

    trades_before = len(trades)
    rejected_before = len(rejected)
    new_opened = process_picks(parsed, data, portfolio, trades, rejected, today_str)
    exits = trades[trades_before:]
    rejected_today = rejected[rejected_before:]

    recompute_totals(portfolio, data["prices"])
    portfolio["last_updated"] = today_str
    portfolio["spy_trend_today"] = data["spy_trend"]

    save_json(PORTFOLIO_FILE, portfolio)
    save_json(TRADES_FILE, trades)
    save_json(REJECTED_FILE, rejected)

    print(f"Opened {len(new_opened)} | Exited {len(exits)} | Rejected {len(rejected_today)} "
          f"| Cash ${portfolio['cash']:,.0f} | Total ${portfolio['total_value']:,.0f}")

    send_email(portfolio, new_opened, exits, rejected_today, data)
    print("Done.")


if __name__ == "__main__":
    main()
