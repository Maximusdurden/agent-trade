#!/usr/bin/env python3
"""Brain (executor) model A/B comparison harness.

Attributes each closed round-trip to the model that produced the DECISION that
opened the position, then compares realized outcomes grouped by model.

Unlike the strategist harness (which attributes by the rule active at entry),
the brain experiment is attributed directly: every executed trade carries a
`decision_id` foreign key into `decisions`, and each decision row now carries a
`model` column (stamped by TradingBrain._stamp_model). So we join
trades.decision_id -> decisions.id and read decisions.model for the BUY that
opened each round-trip.

Read-only. Outputs:
  - reports/brain_ab_report.md   full comparison
  - prints a summary to stdout
"""
import os
import sys
import sqlite3
from collections import defaultdict
from datetime import datetime

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

CLOUD_DB = os.path.join(PROJECT_ROOT, "cloud_downloaded_trading_agent.db")
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)


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


def load_decision_models(db_path=None):
    """Return {decision_id: model} for every decision that carries a model tag.

    Returns {} if the DB predates the brain A/B migration (no `model` column),
    so the report degrades gracefully to "no attributed trips yet".
    """
    if db_path is None:
        db_path = CLOUD_DB
    conn = sqlite3.connect(db_path)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()]
        if "model" not in cols:
            conn.close()
            return {}
        rows = conn.execute(
            "SELECT id, model FROM decisions WHERE model IS NOT NULL AND model != ''"
        ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return {}
    conn.close()
    return {rid: model for rid, model in rows}


def build_round_trips(db_path=None):
    """Build equity round-trips, attributing each to the model of its opening BUY.

    Returns (trips, not_attributed) where each trip carries:
      symbol, open_ts, pnl, pnl_pct, holding_hours, win, model (or None)
    """
    if db_path is None:
        db_path = CLOUD_DB
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT timestamp, symbol, side, qty, filled_avg_price, status, decision_id "
        "FROM trades WHERE status IN ('filled','partially_filled') ORDER BY id ASC"
    ).fetchall()
    conn.close()
    decision_models = load_decision_models(db_path)

    buys = defaultdict(list)
    trips = []
    not_attributed = 0
    for ts, symbol, side, qty, price, _, decision_id in rows:
        symbol = norm(symbol)
        if "/" in symbol:
            continue  # equities only for this experiment
        side = (side or "").lower()
        qty = float(qty or 0); price = float(price or 0)
        if side == "buy":
            buys[symbol].append({"qty": qty, "price": price, "ts": ts,
                                 "model": decision_models.get(decision_id)})
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
                model = b["model"]
                if not model:
                    not_attributed += 1
                trips.append({"symbol": symbol, "open_ts": b["ts"], "pnl": pnl,
                              "pnl_pct": pnl_pct, "holding_hours": hold,
                              "win": pnl > 0, "model": model})
                tmp -= m
                b["qty"] -= m
                if b["qty"] <= 1e-9:
                    buys[symbol].pop(0)
    return trips, not_attributed


def analyze(db_path=None):
    """Load cloud DB and group round-trips by the model that opened them.

    Returns a dict:
      trips          -> list of all equity round-trips (with model, may be None)
      attributed     -> round-trips with a model tag
      not_attributed -> count of pre-experiment / untagged round-trips
      grouped        -> pandas DataFrame grouped by model, or None
      per_ticker     -> pandas DataFrame grouped by (symbol, model), or None
    Pure read-only; reusable by both the file writer and a Discord notifier.
    """
    if db_path is None:
        db_path = CLOUD_DB
    trips, not_attributed = build_round_trips(db_path)
    attributed = [t for t in trips if t["model"]]

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
    print(f"Attributed to a brain A/B model: {len(attributed)}")
    print(f"Not attributed (pre-experiment / untagged): {not_attributed}")
    print()

    lines = ["# Brain Model A/B Report\n",
             f"**Generated:** {datetime.utcnow().isoformat()}Z",
             f"**Source:** `{os.path.basename(CLOUD_DB)}`\n",
             f"- Equity round-trips analyzed: **{len(trips)}**",
             f"- Attributed to a brain A/B model: **{len(attributed)}**",
             f"- Not attributed (pre-experiment/untagged): **{not_attributed}**\n"]

    if grouped is not None:
        lines.append("## Results by model\n")
        lines.append(grouped.to_markdown())
        lines.append("")
        lines.append("> Win% is win rate; expectancy = avg net PnL/trade.")
        print(grouped.to_string())
        print()

        per_ticker = res["per_ticker"]
        lines.append("## Per-ticker by model\n")
        lines.append(per_ticker.to_markdown())
        lines.append("")
        print(per_ticker.to_string())
    else:
        lines.append("No round-trips are attributed to a brain A/B model yet.")
        lines.append("")
        lines.append("This is expected until the brain has run under the A/B "
                     "experiment and logged decisions with a `model` tag.")

    # Caveat
    lines.append("## Caveats\n")
    lines.append("- Attribution is by the **model that opened** the position "
                 "(the BUY decision's `model`), not the model that exited it.")
    lines.append("- Small sample (low RT counts) is not statistically significant — "
                 "collect 2-4 weeks before drawing conclusions.")
    lines.append("- 'None' model = decisions logged before the brain A/B tagging shipped.")

    out = os.path.join(REPORTS_DIR, "brain_ab_report.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nReport written to {out}")


if __name__ == "__main__":
    main()