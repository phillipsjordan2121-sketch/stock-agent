"""
position_checker.py
-------------------
Runs BEFORE main.py in the daily pipeline.

Responsibilities:
  1. Consistency check — ensure portfolio.json internal totals are coherent.
  2. Fetch live prices for all open positions via Finnhub /quote (pc field).
  3. Apply price-based sell rules 1–5:
       Rule 1: Stop-loss  — price dropped ≥ 7% below entry
       Rule 2: Hard exit  — hold_days_actual ≥ hold_days (Claude's suggested hold)
       Rule 3: Profit lock — pnl_pct ≥ 25%
       Rule 4: Trailing stop — price dropped ≥ 5% from intra-hold peak
       Rule 5: Circuit breaker — portfolio total_pnl_pct ≤ -8%
  4. Post-exit fill sweep — if a closed trade's post_exit_price is None
     and ≥ 7 calendar days have elapsed since exit, fetch and store it.
  5. Write updated portfolio.json and trades.json back to disk.

NOTE: Sell rules 6 (catalyst reversal) and 7 (confidence decay) are handled
by Claude's position_verdicts in main.py — NOT here.
"""

import json
import os
import time
import requests
from datetime import date, datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
FINNHUB_KEY       = os.environ.get("FINNHUB_API_KEY", "")
PORTFOLIO_FILE    = "portfolio.json"
TRADES_FILE       = "trades.json"
FINNHUB_SLEEP     = 1.1    # seconds between API calls (free-tier: 60 calls/min)

STOP_LOSS_PCT     = -0.07  # Rule 1: -7% from entry
PROFIT_LOCK_PCT   =  0.25  # Rule 3: +25% from entry
TRAILING_DROP_PCT = -0.05  # Rule 4: -5% from peak
PORTFOLIO_STOP_PCT = -0.08 # Rule 5: circuit breaker


# ── I/O helpers ───────────────────────────────────────────────────────────────

def load_json(path: str, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: str, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── Finnhub helpers ───────────────────────────────────────────────────────────

def fetch_quote(ticker: str) -> float | None:
    """
    Fetch current price from Finnhub /quote.
    Uses 'pc' (previous close) as a safe proxy when market is closed;
    'c' (current price) when market is open.
    Returns None on failure.
    """
    url = "https://finnhub.io/api/v1/quote"
    try:
        r = requests.get(
            url,
            params={"symbol": ticker, "token": FINNHUB_KEY},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        # 'c' = current price; 'pc' = previous close.
        # Use 'c' if non-zero, else fall back to 'pc'.
        current = data.get("c", 0.0)
        return float(current) if current else float(data.get("pc", 0.0)) or None
    except Exception as e:
        print(f"  [WARN] fetch_quote({ticker}) failed: {e}")
        return None


# ── Consistency check ─────────────────────────────────────────────────────────

def run_consistency_check(portfolio: dict) -> dict:
    """
    Recompute total_invested, portfolio_value, total_value, open_position_count
    from the positions dict to catch any drift.
    Does NOT update prices — uses stored entry_price * shares as approximation.
    """
    positions = portfolio.get("positions", {})
    total_invested = 0.0
    open_count     = 0

    for ticker, pos in positions.items():
        shares      = float(pos.get("shares", 0))
        entry_price = float(pos.get("entry_price", 0))
        total_invested += shares * entry_price
        open_count     += 1

    cash = float(portfolio.get("cash", 0.0))
    portfolio["total_invested"]      = round(total_invested, 2)
    portfolio["open_position_count"] = open_count
    # portfolio_value will be recalculated after live prices fetched
    portfolio["total_value"]         = round(cash + total_invested, 2)

    return portfolio


# ── Close a position ──────────────────────────────────────────────────────────

def close_position(
    portfolio: dict,
    trades: list,
    ticker: str,
    current_price: float,
    exit_reason: str,
    today_str: str,
) -> tuple[dict, list]:
    """
    Removes the position from portfolio.positions,
    frees cash (shares * exit_price),
    appends a closed-trade record to trades list.
    Returns updated (portfolio, trades).
    """
    pos = portfolio["positions"].pop(ticker)

    shares      = float(pos["shares"])
    entry_price = float(pos["entry_price"])
    cost_basis  = shares * entry_price
    exit_value  = shares * current_price
    pnl_dollar  = exit_value - cost_basis
    pnl_pct     = pnl_dollar / cost_basis if cost_basis > 0 else 0.0

    portfolio["cash"] = round(portfolio["cash"] + exit_value, 2)

    trade_record = {
        "ticker":               ticker,
        "company":              pos.get("company", ticker),
        "sector":               pos.get("sector", "unknown"),
        "entry_date":           pos.get("entry_date", ""),
        "exit_date":            today_str,
        "entry_price":          entry_price,
        "exit_price":           round(current_price, 4),
        "shares":               round(shares, 6),
        "cost_basis":           round(cost_basis, 2),
        "exit_value":           round(exit_value, 2),
        "pnl_dollar":           round(pnl_dollar, 2),
        "pnl_pct":              round(pnl_pct, 6),
        "hold_days_actual":     (date.fromisoformat(today_str) - date.fromisoformat(pos["entry_date"])).days,
        "hold_days_suggested":  pos.get("hold_days", 3),
        "signals":              pos.get("signals", []),
        "confidence_at_entry":  pos.get("confidence_at_entry", None),
        "exit_reason":          exit_reason,
        "spy_trend_at_entry":   pos.get("spy_trend_at_entry", "unknown"),
        "post_exit_price":      None,
        "post_exit_date":       None,
        "catalyst":             pos.get("catalyst", ""),
        "invalidation":         pos.get("invalidation", ""),
    }

    trades.append(trade_record)
    portfolio["total_closed_trades"] = portfolio.get("total_closed_trades", 0) + 1
    print(f"  [CLOSE] {ticker}: {exit_reason} | pnl={pnl_pct:.2%} | exit_price={current_price:.2f}")

    return portfolio, trades


# ── Post-exit fill sweep ───────────────────────────────────────────────────────

def post_exit_fill_sweep(trades: list) -> list:
    """
    For any closed trade where post_exit_price is None and ≥ 7 calendar days
    have elapsed since exit_date, fetch the current price and store it.
    """
    today = date.today()
    needs_fill = []
    for i, trade in enumerate(trades):
        if trade.get("post_exit_price") is not None:
            continue
        exit_date_str = trade.get("exit_date", "")
        if not exit_date_str:
            continue
        try:
            exit_dt = date.fromisoformat(exit_date_str)
        except ValueError:
            continue
        days_elapsed = (today - exit_dt).days
        if days_elapsed >= 7:
            needs_fill.append(i)

    if not needs_fill:
        return trades

    print(f"  Post-exit fill sweep: {len(needs_fill)} trade(s) need fill.")
    for i in needs_fill:
        ticker = trades[i]["ticker"]
        price  = fetch_quote(ticker)
        time.sleep(FINNHUB_SLEEP)
        if price:
            trades[i]["post_exit_price"] = round(price, 4)
            trades[i]["post_exit_date"]  = today.isoformat()
            print(f"    Filled post-exit price for {ticker}: {price:.2f}")
        else:
            print(f"    [WARN] Could not fetch post-exit price for {ticker}")

    return trades


# ── Main sell-rule evaluation ─────────────────────────────────────────────────

def evaluate_price_rules(portfolio: dict, trades: list) -> tuple[dict, list]:
    """
    Iterates all open positions, fetches live prices, applies rules 1–5.
    Rule 5 (circuit breaker) checked once after all prices known.
    Returns updated (portfolio, trades).
    """
    today_str  = date.today().isoformat()
    positions  = portfolio.get("positions", {})
    tickers    = list(positions.keys())

    if not tickers:
        return portfolio, trades

    # ── Fetch all live prices first ──
    live_prices = {}
    for ticker in tickers:
        price = fetch_quote(ticker)
        live_prices[ticker] = price
        time.sleep(FINNHUB_SLEEP)

    # ── Update portfolio_value with live prices ──
    portfolio_value = 0.0
    for ticker, pos in positions.items():
        price = live_prices.get(ticker) or float(pos.get("entry_price", 0))
        portfolio_value += float(pos["shares"]) * price
    portfolio["portfolio_value"] = round(portfolio_value, 2)

    cash = float(portfolio.get("cash", 0.0))
    total_value = cash + portfolio_value
    portfolio["total_value"] = round(total_value, 2)

    starting_capital = float(portfolio.get("starting_capital", 100000.0))
    total_pnl_pct    = (total_value - starting_capital) / starting_capital
    portfolio["total_pnl_pct"] = round(total_pnl_pct, 6)

    # ── Rule 5: Portfolio circuit breaker ──
    if total_pnl_pct <= PORTFOLIO_STOP_PCT and not portfolio.get("paused", False):
        portfolio["paused"] = True
        print(f"  [CIRCUIT BREAKER] Portfolio down {total_pnl_pct:.2%} — paused=True. No new picks today.")

    # ── Per-position rules 1–4 ──
    to_close = []  # collect tickers to close (avoid mutating dict during iteration)

    for ticker in tickers:
        pos   = positions.get(ticker)
        if pos is None:
            continue

        price = live_prices.get(ticker)
        if price is None:
            print(f"  [WARN] No price for {ticker}, skipping sell rules.")
            continue

        entry_price = float(pos["entry_price"])
        shares      = float(pos["shares"])
        entry_date  = pos.get("entry_date", today_str)
        hold_days_suggested = int(pos.get("hold_days", 3))

        try:
            hold_days_actual = (date.fromisoformat(today_str) - date.fromisoformat(entry_date)).days
        except ValueError:
            hold_days_actual = 0

        # Update peak price (for trailing stop)
        peak_price = float(pos.get("peak_price", entry_price))
        if price > peak_price:
            peak_price = price
            positions[ticker]["peak_price"] = round(peak_price, 4)

        pnl_pct        = (price - entry_price) / entry_price if entry_price > 0 else 0.0
        trail_from_peak = (price - peak_price) / peak_price if peak_price > 0 else 0.0

        reason = None

        # Rule 1: Stop-loss
        if pnl_pct <= STOP_LOSS_PCT:
            reason = f"stop_loss: pnl={pnl_pct:.2%}"

        # Rule 2: Hard hold-day exit (only if not already flagged)
        elif hold_days_actual >= hold_days_suggested:
            reason = f"hold_expired: {hold_days_actual}d >= {hold_days_suggested}d suggested"

        # Rule 3: Profit lock
        elif pnl_pct >= PROFIT_LOCK_PCT:
            reason = f"profit_lock: pnl={pnl_pct:.2%}"

        # Rule 4: Trailing stop
        elif trail_from_peak <= TRAILING_DROP_PCT:
            reason = f"trailing_stop: {trail_from_peak:.2%} from peak={peak_price:.2f}"

        if reason:
            to_close.append((ticker, price, reason))

    # ── Execute closes ──
    for ticker, price, reason in to_close:
        portfolio, trades = close_position(portfolio, trades, ticker, price, reason, today_str)

    # ── Recompute totals after closes ──
    remaining_positions = portfolio.get("positions", {})
    new_portfolio_value = 0.0
    for ticker, pos in remaining_positions.items():
        price = live_prices.get(ticker) or float(pos.get("entry_price", 0))
        new_portfolio_value += float(pos["shares"]) * price

    portfolio["portfolio_value"]     = round(new_portfolio_value, 2)
    portfolio["open_position_count"] = len(remaining_positions)
    cash = float(portfolio.get("cash", 0.0))
    new_total = cash + new_portfolio_value
    portfolio["total_value"]  = round(new_total, 2)
    portfolio["total_invested"] = round(new_portfolio_value, 2)
    new_pnl_pct = (new_total - starting_capital) / starting_capital
    portfolio["total_pnl_pct"] = round(new_pnl_pct, 6)
    portfolio["last_updated"]  = today_str

    return portfolio, trades


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if not FINNHUB_KEY:
        print("position_checker.py: FINNHUB_API_KEY not set — skipping price checks.")
        return

    portfolio = load_json(PORTFOLIO_FILE, {})
    trades    = load_json(TRADES_FILE,    [])

    if not portfolio:
        print("position_checker.py: portfolio.json missing or empty — skipping.")
        return

    print("position_checker.py: running consistency check...")
    portfolio = run_consistency_check(portfolio)

    open_count = len(portfolio.get("positions", {}))
    print(f"position_checker.py: {open_count} open position(s).")

    if open_count > 0:
        print("position_checker.py: evaluating price-based sell rules...")
        portfolio, trades = evaluate_price_rules(portfolio, trades)

    print("position_checker.py: running post-exit fill sweep...")
    trades = post_exit_fill_sweep(trades)

    save_json(PORTFOLIO_FILE, portfolio)
    save_json(TRADES_FILE, trades)

    remaining = len(portfolio.get("positions", {}))
    print(f"position_checker.py: done. {remaining} position(s) remaining.")


if __name__ == "__main__":
    main()
