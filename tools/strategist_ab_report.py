#!/usr/bin/env python3
"""Strategist model A/B comparison harness.

Attributes each closed round-trip to the model that authored the ACTIVE strategy
rule at the round-trip's entry time, then compares realized outcomes grouped by
authoring model.

The strategist logs each rule with a `strategy_version` of the form
`v<ts>|model=<model-id>` (see core/strategist.py). We map each symbol's rule
history -> (timestamp, model), then for every round-trip find the most recent
rule authored BEFORE its entry, and attribute the trade to that model.

Read-only. Outputs:
  - reports/strategist_ab_report.md   full comparison
  - prints a summary to stdout
"""
import os
import sys
import re
import sqlite3
from collections import defaultdict
from datetime import datetime

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

CLOUD_DB = os.path.join(PROJECT_ROOT, "cloud_downloaded_trading_agent.db")
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

MODEL_RE = re.compile(r"\|\s*model=([^|]+)\s*$")


def norm(s):
    s = (s or "").strip().upper().replace("-", "/")
    return s


def parse_dt(ts):
    if not ts:
        return None
    try:
        dt = pd.to_datetime(ts)
        if dt.tzinfo is None:
            dt = dt.tz_localize("UTC")
        else:
            dt = dt.tz_convert("UTC")
        return dt
    except Exception:
        return None


def load_strategy_models(db_path=None):
    """Return {ticker: [(ts, model, rules)]} sorted by ts ASC."""
    if db_path is None:
        db_path = CLOUD_DB
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT timestamp, ticker, todays_rules, strategy_version FROM strategy_history ORDER BY timestamp ASC"
    ).fetchall()
    conn.close()
    by_ticker = defaultdict(list)
    for ts, ticker, rules, ver in rows:
        m = MODEL_RE.search(ver or "")
        model = m.group(1) if m else "unknown"
        by_ticker[ticker.upper()].append((parse_dt(ts), model, rules))
    return by_ticker


def model_at_entry(hist, ts):
    """Find the model of the most recent rule authored before ts, else None."""
    prior = None
    for rts, model, _ in hist:
        if rts is not None and rts <= ts:
            prior = model
        else:
            break
    return prior


def build_round_trips(db_path=None):
    if db_path is None:
        db_path = CLOUD_DB
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT timestamp, symbol, side, qty, filled_avg_price, status FROM trades "
        "WHERE status IN ('filled','partially_filled') ORDER BY id ASC"
    ).fetchall()
    conn.close()
    buys = defaultdict(list)
    trips = []
    for ts, symbol, side, qty, price, _ in rows:
        symbol = norm(symbol)
        if "/" in symbol:
            continue  # equities only for this experiment
        side = (side or "").lower()
        qty = float(qty or 0); price = float(price or 0)
        if side == "buy":
            buys[symbol].append({"qty": qty, "price": price, "ts": ts})
        elif side == "sell":
            tmp = qty
            while tmp > 0 and buys.get(symbol):
                b = buys[symbol][0]
                m = min(tmp, b["qty"])
                entry = b["price"] or 0
                pnl = m * (price - entry)
                pnl_pct = ((price - entry) / entry * 100) if entry else 0
                t_open = parse_dt(b["ts"]); t_close = parse_dt(ts)
                hold = (t_close - t_open).total_seconds() / 3600 if t_open and t_close else 0
                trips.append({"symbol": symbol, "open_ts": b["ts"], "pnl": pnl,
                              "pnl_pct": pnl_pct, "holding_hours": hold, "win": pnl > 0})
                tmp -= m
                b["qty"] -= m
                if b["qty"] <= 1e-9:
                    buys[symbol].pop(0)
    return trips


def analyze(db_path=None):
    """Load cloud DB and attribute round-trips to authoring A/B model.

    Returns a dict:
      trips          -> list of all equity round-trips (with open_ts)
      attributed     -> list of round-trips attributed to an A/B-tagged model (with model)
      not_attributed -> count of pre-experiment / untagged round-trips
      grouped        -> pandas DataFrame grouped by model (rt, pnl, win%, avg hold,
                        largest win/loss), or None if no attributed trades
      per_ticker     -> pandas DataFrame grouped by (symbol, model), or None
    Pure read-only; reusable by both the file writer and the Discord notifier.
    """
    if db_path is None:
        db_path = CLOUD_DB
    hist = load_strategy_models(db_path)
    trips = build_round_trips(db_path)
    # Restrict to rules that carry an A/B model tag (skip pre-experiment 'unknown' history).
    attributed = []
    not_attributed = 0
    for t in trips:
        t_open = parse_dt(t["open_ts"])
        model = model_at_entry(hist.get(t["symbol"], []), t_open)
        if not model or model == "unknown":
            not_attributed += 1
            continue
        t["model"] = model
        attributed.append(t)

    grouped = None
    per_ticker = None
    if attributed:
        df = pd.DataFrame(attributed)
        grouped = df.groupby("model").agg(
            rt=("pnl", "size"), pnl=("pnl", "sum"), win=("win", "mean"),
            avg_hold_h=("holding_hours", "mean"),
            largest_win=("pnl", "max"), largest_loss=("pnl", "min")
        ).sort_values("pnl", ascending=False)
        per_ticker = df.groupby(["symbol", "model"]).agg(
            rt=("pnl", "size"), pnl=("pnl", "sum"), win=("win", "mean")
        ).sort_values(["symbol", "model"])

    return {
        "trips": trips,
        "attributed": attributed,
        "not_attributed": not_attributed,
        "grouped": grouped,
        "per_ticker": per_ticker,
    }


def main():
    res = analyze()
    trips = res["trips"]
    attributed = res["attributed"]
    not_attributed = res["not_attributed"]
    grouped = res["grouped"]

    print(f"Total equity round-trips: {len(trips)}")
    print(f"Attributed to an A/B strategist model: {len(attributed)}")
    print(f"Not attributed (pre-experiment / untagged): {not_attributed}")
    print()

    lines = ["# Strategist Model A/B Report\n",
             f"**Generated:** {datetime.utcnow().isoformat()}Z",
             f"**Source:** `{os.path.basename(CLOUD_DB)}`\n",
             f"- Equity round-trips analyzed: **{len(trips)}**",
             f"- Attributed to an A/B-tagged strategist rule: **{len(attributed)}**",
             f"- Not attributed (pre-experiment/untagged): **{not_attributed}**\n"]

    if grouped is not None:
        # `grouped` is already sorted by pnl desc internally already
        lines.append("## Results by authoring model\n")
        lines.append(grouped.to_markdown())
        lines.append("")
        lines.append("> Win% is win rate; expectancy = avg net PnL/trade.")
        print(grouped.to_string())
        print()

        # Per-ticker split too
        per_ticker = res["per_ticker"]
        lines.append("## Per-ticker by model\n")
        lines.append(per_ticker.to_markdown())
        lines.append("")
        print(per_ticker.to_string())
    else:
        lines.append("No round-trips are attributed to an A/B-tagged rule yet.")
        lines.append("")
        lines.append("This is expected until the strategist has run under the A/B "
                     "experiment and logged some rules with `|model=...` tags.")

    # Caveat
    lines.append("## Caveats\n")
    lines.append("- Attribution is by the **rule active at entry**, not the rule that "
                 "exited the trade; exits are often broker TP/SL.")
    lines.append("- Small sample (low RT counts) is not statistically significant — "
                 "collect 2-4 weeks before drawing conclusions.")
    lines.append("- 'unknown' model = rules logged before the A/B tagging shipped.")

    out = os.path.join(REPORTS_DIR, "strategist_ab_report.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nReport written to {out}")


if __name__ == "__main__":
    main()