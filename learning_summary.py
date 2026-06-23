"""
learning_summary.py
-------------------
Reads trades.json and rejected.json.
Computes Bayesian-updated signal win rates, calibration data,
recency-decayed performance by confidence band, and sector/hold-day stats.
Writes learning_context.txt.
NO Anthropic API calls are made here.
"""

import json
import math
from datetime import date, datetime
from collections import defaultdict

# ── Constants ─────────────────────────────────────────────────────────────────
TRADES_FILE          = "trades.json"
REJECTED_FILE        = "rejected.json"
LEARNING_CONTEXT_OUT = "learning_context.txt"

PRIOR_N   = 10   # virtual prior trades per signal (Bayesian regularisation)
PRIOR_WIN  = 0.5  # prior assumed win-rate

RECENCY_LAMBDA = 0.015   # decay constant: exp(-0.015 * days_since_exit)
CALIBRATION_MIN_N = 4    # min weighted trades before band is considered calibrated

CONFIDENCE_BANDS = [
    (90, 100, "90-100%"),
    (80, 89,  "80-89%"),
    (70, 79,  "70-79%"),
    (60, 69,  "60-69%"),
]

SIGNAL_NAMES = [
    "analyst_upgrade",
    "analyst_upgrade_strong",
    "price_target_raised",
    "price_target_raised_major",
    "earnings_beat",
    "earnings_beat_minor",
    "guidance_raise",
    "company_specific_news",
    "momentum",
    "secondary_beneficiary",
    "sector_tailwind",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_json(path: str, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def recency_weight(exit_date_str: str) -> float:
    """Exponential recency weight based on calendar days since exit."""
    try:
        exit_dt = date.fromisoformat(exit_date_str)
        days_ago = (date.today() - exit_dt).days
        return math.exp(-RECENCY_LAMBDA * max(days_ago, 0))
    except (ValueError, TypeError):
        return 0.1


def is_win(trade: dict) -> bool:
    """A trade is a win if pnl_pct > 0."""
    pnl = trade.get("pnl_pct")
    if pnl is None:
        return False
    return float(pnl) > 0.0


# ── Core computation ──────────────────────────────────────────────────────────

def compute_signal_win_rates(trades: list) -> dict:
    """
    For each signal, accumulate weighted wins/losses across all trades
    that included that signal, then apply Bayesian update with PRIOR_N.
    Returns {signal_name: {"raw_n": int, "weighted_win_rate": float, "calibrated": bool}}
    """
    raw_counts   = defaultdict(lambda: {"wins": 0, "losses": 0, "w_wins": 0.0, "w_total": 0.0})

    for trade in trades:
        signals   = trade.get("signals", [])
        exit_date = trade.get("exit_date", "")
        w         = recency_weight(exit_date)
        win       = is_win(trade)

        for sig in signals:
            if sig not in SIGNAL_NAMES:
                continue
            raw_counts[sig]["w_total"] += w
            raw_counts[sig]["wins" if win else "losses"] += 1
            if win:
                raw_counts[sig]["w_wins"] += w

    results = {}
    for sig in SIGNAL_NAMES:
        c    = raw_counts[sig]
        n    = c["wins"] + c["losses"]
        wt   = c["w_total"]
        ww   = c["w_wins"]

        # Bayesian posterior: (prior_wins + weighted_wins) / (prior_n + weighted_total)
        posterior = (PRIOR_N * PRIOR_WIN + ww) / (PRIOR_N + wt) if (PRIOR_N + wt) > 0 else PRIOR_WIN

        results[sig] = {
            "raw_n":           n,
            "weighted_win_rate": round(posterior, 4),
            "calibrated":      n >= CALIBRATION_MIN_N,
        }

    return results


def compute_band_performance(trades: list) -> dict:
    """
    For each confidence band, compute weighted win-rate and avg pnl_pct.
    Returns {band_label: {"weighted_win_rate": float, "avg_pnl_pct": float,
                          "weighted_n": float, "calibrated": bool}}
    """
    band_stats = {label: {"w_wins": 0.0, "w_total": 0.0, "w_pnl": 0.0} for _, _, label in CONFIDENCE_BANDS}

    for trade in trades:
        conf = trade.get("confidence_at_entry")
        if conf is None:
            continue
        exit_date = trade.get("exit_date", "")
        w         = recency_weight(exit_date)
        win       = is_win(trade)
        pnl       = float(trade.get("pnl_pct", 0.0))

        for lo, hi, label in CONFIDENCE_BANDS:
            if lo <= int(conf) <= hi:
                band_stats[label]["w_total"] += w
                band_stats[label]["w_pnl"]   += w * pnl
                if win:
                    band_stats[label]["w_wins"] += w
                break

    results = {}
    for _, _, label in CONFIDENCE_BANDS:
        s  = band_stats[label]
        wt = s["w_total"]
        results[label] = {
            "weighted_win_rate": round(s["w_wins"] / wt, 4) if wt > 0 else None,
            "avg_pnl_pct":       round(s["w_pnl"]  / wt, 4) if wt > 0 else None,
            "weighted_n":        round(wt, 2),
            "calibrated":        wt >= CALIBRATION_MIN_N,
        }

    return results


def compute_sector_stats(trades: list) -> dict:
    """Win-rate by sector (unweighted, raw counts)."""
    sector_stats = defaultdict(lambda: {"wins": 0, "total": 0})
    for trade in trades:
        sector = trade.get("sector", "unknown")
        if is_win(trade):
            sector_stats[sector]["wins"] += 1
        sector_stats[sector]["total"] += 1

    results = {}
    for sector, s in sector_stats.items():
        results[sector] = {
            "win_rate": round(s["wins"] / s["total"], 4) if s["total"] > 0 else None,
            "total":    s["total"],
        }
    return results


def compute_hold_day_stats(trades: list) -> dict:
    """Avg pnl_pct grouped by hold_days bucket."""
    buckets = defaultdict(list)
    for trade in trades:
        hold = trade.get("hold_days_actual")
        pnl  = trade.get("pnl_pct")
        if hold is None or pnl is None:
            continue
        hold = int(hold)
        bucket = f"{hold}d" if hold <= 5 else "6d+"
        buckets[bucket].append(float(pnl))

    results = {}
    for bucket, pnls in buckets.items():
        results[bucket] = {
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 4),
            "n":           len(pnls),
        }
    return results


def compute_rejected_stats(rejected: list) -> dict:
    """Most-common rejection reasons (for self-awareness)."""
    reason_counts = defaultdict(int)
    for r in rejected:
        reason = r.get("rejection_reason", "unknown")
        reason_counts[reason] += 1

    sorted_reasons = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)
    return dict(sorted_reasons[:10])


def compute_exit_reason_stats(trades: list) -> dict:
    """Win/loss breakdown by exit reason."""
    stats = defaultdict(lambda: {"wins": 0, "losses": 0})
    for trade in trades:
        reason = trade.get("exit_reason", "unknown")
        if is_win(trade):
            stats[reason]["wins"] += 1
        else:
            stats[reason]["losses"] += 1

    results = {}
    for reason, s in stats.items():
        total = s["wins"] + s["losses"]
        results[reason] = {
            "win_rate": round(s["wins"] / total, 4) if total > 0 else None,
            "total": total,
        }
    return results


# ── Calibration adjustment suggestions ───────────────────────────────────────

def build_calibration_notes(band_perf: dict) -> list:
    """
    If a calibrated band's actual win-rate diverges from what we'd expect
    for that allocation tier, note it so Claude can factor this in.
    """
    notes = []
    EXPECTED = {
        "90-100%": 0.70,
        "80-89%":  0.60,
        "70-79%":  0.50,
        "60-69%":  0.40,
    }
    for label, data in band_perf.items():
        if not data["calibrated"] or data["weighted_win_rate"] is None:
            notes.append(f"  {label}: insufficient data (weighted_n={data['weighted_n']:.1f})")
            continue
        actual   = data["weighted_win_rate"]
        expected = EXPECTED.get(label, 0.50)
        diff     = actual - expected
        if abs(diff) >= 0.10:
            direction = "OVER-performing" if diff > 0 else "UNDER-performing"
            notes.append(
                f"  {label}: {direction} (actual={actual:.1%}, expected≥{expected:.1%}, "
                f"n={data['weighted_n']:.1f}, avg_pnl={data['avg_pnl_pct']:.2%})"
            )
        else:
            notes.append(
                f"  {label}: on-track (actual={actual:.1%}, n={data['weighted_n']:.1f}, "
                f"avg_pnl={data['avg_pnl_pct']:.2%})"
            )
    return notes


# ── Output formatter ──────────────────────────────────────────────────────────

def format_learning_context(
    trades: list,
    signal_wr: dict,
    band_perf: dict,
    sector_stats: dict,
    hold_stats: dict,
    rejected_reasons: dict,
    exit_reason_stats: dict,
) -> str:
    total  = len(trades)
    wins   = sum(1 for t in trades if is_win(t))
    losses = total - wins
    overall_wr = wins / total if total > 0 else 0.0

    pnls = [float(t["pnl_pct"]) for t in trades if t.get("pnl_pct") is not None]
    avg_pnl = sum(pnls) / len(pnls) if pnls else 0.0

    lines = [
        "=== SELF-LEARNING SUMMARY ===",
        f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        f"Total closed trades: {total}  |  Wins: {wins}  Losses: {losses}  |  Win-rate: {overall_wr:.1%}",
        f"Average PnL per trade: {avg_pnl:.2%}",
        "",
        "--- SIGNAL WIN RATES (Bayesian, recency-weighted) ---",
        "Format: signal -> posterior_win_rate (raw_n trades) [CALIBRATED / UNCALIBRATED]",
    ]

    for sig in SIGNAL_NAMES:
        d    = signal_wr[sig]
        flag = "CALIBRATED" if d["calibrated"] else "uncalibrated"
        lines.append(
            f"  {sig:35s}: {d['weighted_win_rate']:.1%}  (n={d['raw_n']})  [{flag}]"
        )

    lines += [
        "",
        "--- CONFIDENCE BAND CALIBRATION ---",
        "Interpretation: use these to adjust sizing confidence up/down within each band.",
    ]
    cal_notes = build_calibration_notes(band_perf)
    lines += cal_notes

    lines += [
        "",
        "--- SECTOR PERFORMANCE (raw win-rate) ---",
    ]
    for sector, s in sorted(sector_stats.items(), key=lambda x: -(x[1]["total"])):
        wr_str = f"{s['win_rate']:.1%}" if s["win_rate"] is not None else "N/A"
        lines.append(f"  {sector:20s}: {wr_str}  (n={s['total']})")

    lines += [
        "",
        "--- HOLD DURATION PERFORMANCE ---",
    ]
    for bucket, s in sorted(hold_stats.items()):
        lines.append(f"  {bucket}: avg_pnl={s['avg_pnl_pct']:.2%}  (n={s['n']})")

    lines += [
        "",
        "--- EXIT REASON PERFORMANCE ---",
    ]
    for reason, s in sorted(exit_reason_stats.items(), key=lambda x: -(x[1]["total"])):
        wr_str = f"{s['win_rate']:.1%}" if s["win_rate"] is not None else "N/A"
        lines.append(f"  {reason:30s}: win_rate={wr_str}  (n={s['total']})")

    lines += [
        "",
        "--- TOP REJECTION REASONS ---",
    ]
    for reason, count in rejected_reasons.items():
        lines.append(f"  {reason:40s}: {count}")

    lines += [
        "",
        "=== END SUMMARY ===",
    ]

    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    trades   = load_json(TRADES_FILE,   [])
    rejected = load_json(REJECTED_FILE, [])

    if not trades:
        # Day-1 fallback: no trade history yet
        context = (
            "=== SELF-LEARNING SUMMARY ===\n"
            "Insufficient trade history for calibration. Using base signal weights.\n"
            "Apply no calibration adjustments today.\n"
            "=== END SUMMARY ==="
        )
        with open(LEARNING_CONTEXT_OUT, "w") as f:
            f.write(context)
        print("learning_summary.py: no trade history — wrote fallback context.")
        return

    signal_wr        = compute_signal_win_rates(trades)
    band_perf        = compute_band_performance(trades)
    sector_stats     = compute_sector_stats(trades)
    hold_stats       = compute_hold_day_stats(trades)
    rejected_reasons = compute_rejected_stats(rejected)
    exit_reason_stats = compute_exit_reason_stats(trades)

    context = format_learning_context(
        trades, signal_wr, band_perf, sector_stats,
        hold_stats, rejected_reasons, exit_reason_stats,
    )

    with open(LEARNING_CONTEXT_OUT, "w") as f:
        f.write(context)

    total = len(trades)
    wins  = sum(1 for t in trades if is_win(t))
    print(f"learning_summary.py: processed {total} trades ({wins} wins). Context written.")


if __name__ == "__main__":
    main()
