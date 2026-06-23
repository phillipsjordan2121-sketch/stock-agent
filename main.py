import os
import json
import smtplib
import requests
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import anthropic

# ── Config ────────────────────────────────────────────────────────────────────
FINNHUB_API_KEY   = os.environ["FINNHUB_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_USER        = os.environ["GMAIL_USER"]        # your gmail address
GMAIL_APP_PASS    = os.environ["GMAIL_APP_PASS"]    # gmail app password
RECIPIENT_EMAIL   = os.environ.get("RECIPIENT_EMAIL", GMAIL_USER)

CONFIDENCE_THRESHOLD = 80  # only send picks at or above this %

# Tech/AI/Semi tickers to pull analyst data for
WATCHLIST = [
    "NVDA", "AMD", "TSMC", "AVGO", "MRVL", "QCOM", "INTC", "MU", "AMAT",
    "LRCX", "KLAC", "ASML", "ARM", "SMCI", "DELL", "HPE",
    "MSFT", "GOOGL", "AMZN", "META", "ORCL", "CRM", "SNOW", "PLTR",
    "PANW", "NET", "CRWD", "NOW", "DDOG", "MDB", "AI", "IONQ",
]

PROMPT = """You are a short-term trading analyst covering the Tech, AI, and Semiconductor sector. Analyze the news and analyst actions from the last 24 hours provided below. Identify short-term buy opportunities (1–5 day holds) only. Focus on: AI chip plays (Nvidia, AMD, TSMC, Broadcom, Marvell, etc.), AI software with strong momentum, semiconductor equipment, and cloud infrastructure.

For each opportunity, output exactly this format:
TICKER | Company | Confidence: X% | Hold: X–X days | Catalyst: [one sentence citing the specific news] | Edge: [why the market might not have fully priced this in yet] | Invalidation: [1–2 sentences on what would signal early exit within the hold window]

Requirements:
— Every pick must be anchored to a specific catalyst from the provided data — no generic sector tailwinds
— Confidence % should reflect both catalyst strength and how cleanly the setup fits a 1–5 day hold
— The Edge field must identify a concrete mispricing reason: delayed reaction, overlooked secondary beneficiary, analyst revision lag, low retail awareness, etc. — not "market hasn't caught up yet" as a standalone claim; name the specific mechanism
— Rank picks by confidence, highest first
— If a catalyst is strong but the hold window extends beyond 5 days, exclude it
— If two tickers share the same catalyst, include both only if each has a distinct, independently justifiable Edge — state what makes each Edge different from the other
— For each pick, also include a brief note (1–2 sentences) on what would invalidate the thesis within the hold window (conditions that would signal early exit)
No market commentary. No caveats. No stocks to avoid. No preamble. No postamble. Output the picks and nothing else.

NEWS AND ANALYST ACTIONS FROM THE LAST 24 HOURS:
{news_data}"""


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_market_news():
    """Pull general tech/AI news from Finnhub."""
    url = "https://finnhub.io/api/v1/news"
    params = {"category": "technology", "token": FINNHUB_API_KEY}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    articles = r.json()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent = [a for a in articles if datetime.fromtimestamp(a.get("datetime", 0), tz=timezone.utc) >= cutoff]

    lines = []
    for a in recent[:40]:  # cap at 40 articles to stay within token budget
        lines.append(f"[NEWS] {a.get('headline', '')} — {a.get('summary', '')[:200]}")
    return lines


def fetch_company_news(ticker):
    """Pull ticker-specific news from Finnhub."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d")
    url = "https://finnhub.io/api/v1/company-news"
    params = {"symbol": ticker, "from": yesterday, "to": today, "token": FINNHUB_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        articles = r.json()
        lines = []
        for a in articles[:3]:  # top 3 per ticker
            lines.append(f"[{ticker} NEWS] {a.get('headline', '')} — {a.get('summary', '')[:150]}")
        return lines
    except Exception:
        return []


def fetch_analyst_recommendations(ticker):
    """Pull latest analyst recommendations for a ticker."""
    url = "https://finnhub.io/api/v1/stock/recommendation"
    params = {"symbol": ticker, "token": FINNHUB_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data:
            return []
        latest = data[0]
        return [
            f"[{ticker} ANALYST] {latest.get('period','')}: "
            f"Buy={latest.get('buy',0)}, Hold={latest.get('hold',0)}, "
            f"Sell={latest.get('sell',0)}, StrongBuy={latest.get('strongBuy',0)}"
        ]
    except Exception:
        return []


def fetch_price_targets(ticker):
    """Pull consensus price target for a ticker."""
    url = "https://finnhub.io/api/v1/stock/price-target"
    params = {"symbol": ticker, "token": FINNHUB_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data or not data.get("targetMean"):
            return []
        return [
            f"[{ticker} TARGET] Mean=${data['targetMean']:.2f}, "
            f"High=${data.get('targetHigh', 0):.2f}, "
            f"Low=${data.get('targetLow', 0):.2f}, "
            f"Last updated: {data.get('lastUpdated', 'N/A')}"
        ]
    except Exception:
        return []


def build_news_data():
    """Aggregate all data into a single context string."""
    print("Fetching market news...")
    all_lines = fetch_market_news()

    print(f"Fetching data for {len(WATCHLIST)} tickers...")
    for ticker in WATCHLIST:
        all_lines += fetch_company_news(ticker)
        all_lines += fetch_analyst_recommendations(ticker)
        all_lines += fetch_price_targets(ticker)

    return "\n".join(all_lines)


# ── AI analysis ───────────────────────────────────────────────────────────────

def run_analysis(news_data: str) -> str:
    """Send data to Claude Haiku and get picks back."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": PROMPT.format(news_data=news_data)
        }]
    )
    return message.content[0].text


# ── Filtering ─────────────────────────────────────────────────────────────────

def parse_confidence(line: str) -> int:
    """Extract confidence % from a pick line."""
    try:
        idx = line.lower().index("confidence:")
        fragment = line[idx + len("confidence:"):idx + 20]
        num = ""
        for ch in fragment:
            if ch.isdigit():
                num += ch
            elif num:
                break
        return int(num) if num else 0
    except (ValueError, IndexError):
        return 0


def filter_picks(raw_output: str) -> list[str]:
    """Keep only picks at or above the confidence threshold."""
    picks = []
    for line in raw_output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        confidence = parse_confidence(line)
        if confidence >= CONFIDENCE_THRESHOLD:
            picks.append((confidence, line))
    picks.sort(key=lambda x: x[0], reverse=True)
    return [p[1] for p in picks]


# ── Email ─────────────────────────────────────────────────────────────────────

def format_email_html(picks: list[str]) -> str:
    today = datetime.now().strftime("%A, %B %d, %Y")
    rows = ""
    for pick in picks:
        parts = [p.strip() for p in pick.split("|")]
        ticker   = parts[0] if len(parts) > 0 else "—"
        company  = parts[1] if len(parts) > 1 else "—"
        conf     = parts[2] if len(parts) > 2 else "—"
        hold     = parts[3] if len(parts) > 3 else "—"
        catalyst = parts[4] if len(parts) > 4 else "—"
        edge     = parts[5] if len(parts) > 5 else "—"
        invalid  = parts[6] if len(parts) > 6 else "—"

        conf_num = parse_confidence(pick)
        conf_color = "#16a34a" if conf_num >= 90 else "#ca8a04" if conf_num >= 80 else "#dc2626"

        rows += f"""
        <div style="background:#fff;border-radius:12px;padding:20px 24px;margin-bottom:16px;border:1px solid #e5e7eb;">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;">
            <span style="font-size:22px;font-weight:800;color:#111;letter-spacing:-0.5px;">{ticker}</span>
            <span style="font-size:14px;color:#6b7280;">{company}</span>
            <span style="margin-left:auto;background:{conf_color}15;color:{conf_color};font-size:13px;font-weight:700;border-radius:20px;padding:3px 12px;">{conf}</span>
          </div>
          <table style="width:100%;font-size:13px;border-collapse:collapse;">
            <tr><td style="color:#9ca3af;padding:4px 0;width:90px;vertical-align:top;">Hold</td><td style="color:#111;padding:4px 0;">{hold}</td></tr>
            <tr><td style="color:#9ca3af;padding:4px 0;vertical-align:top;">Catalyst</td><td style="color:#111;padding:4px 0;">{catalyst.replace("Catalyst: ","")}</td></tr>
            <tr><td style="color:#9ca3af;padding:4px 0;vertical-align:top;">Edge</td><td style="color:#111;padding:4px 0;">{edge.replace("Edge: ","")}</td></tr>
            <tr><td style="color:#9ca3af;padding:4px 0;vertical-align:top;">Exit if</td><td style="color:#dc2626;padding:4px 0;">{invalid.replace("Invalidation: ","")}</td></tr>
          </table>
        </div>"""

    return f"""
    <html><body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
      <div style="max-width:600px;margin:0 auto;padding:32px 16px;">
        <div style="margin-bottom:24px;">
          <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;color:#6b7280;margin-bottom:6px;">Tech · AI · Semiconductors</div>
          <div style="font-size:26px;font-weight:800;color:#111;letter-spacing:-0.5px;">Morning Picks</div>
          <div style="font-size:14px;color:#9ca3af;margin-top:4px;">{today} · {len(picks)} pick{"s" if len(picks) != 1 else ""} above {CONFIDENCE_THRESHOLD}% confidence</div>
        </div>
        {rows}
        <div style="font-size:11px;color:#d1d5db;margin-top:24px;line-height:1.6;">
          This is automated analysis for informational purposes only. Not financial advice. Always do your own research before trading.
        </div>
      </div>
    </html></body>"""


def send_email(picks: list[str]):
    subject = f"📈 {len(picks)} High-Confidence Pick{'s' if len(picks) != 1 else ''} — {datetime.now().strftime('%b %d')}"
    html = format_email_html(picks)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASS)
        server.sendmail(GMAIL_USER, RECIPIENT_EMAIL, msg.as_string())

    print(f"Email sent: {subject}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Stock agent starting...")

    news_data = build_news_data()
    print(f"Collected {len(news_data.splitlines())} data points.")

    print("Running Claude analysis...")
    raw = run_analysis(news_data)
    print("Raw output:\n", raw)

    picks = filter_picks(raw)
    print(f"\n{len(picks)} pick(s) cleared {CONFIDENCE_THRESHOLD}% threshold.")

    if picks:
        send_email(picks)
    else:
        print("No picks cleared the threshold today. No email sent.")

    print("Done.")


if __name__ == "__main__":
    main()
