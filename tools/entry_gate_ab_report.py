#!/usr/bin/env python3
"""Equity entry-gate A/B comparison harness.

Attributes each closed equity round-trip to the RSI entry-gate threshold that
was active when the position was OPENED, then compares realized outcomes
grouped by gate value.

The guardrail stamps the active gate value (e.g. "rsi_max=50" or "rsi_max=45")
onto each decision (decisions.entry_gate), which flows to the trades table via
decision_id. We join trades.decision_id -> decisions.id and read
decisions.entry_gate for the BUY that opened each round-trip.

This directly tests the equity edge: does a tighter gate (e.g. 45) beat the
current 50? Read-only. Outputs:
  - reports/entry_gate_ab_report.md   full comparison
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


def load_decision_gates(db_path=None):
    """Return {decision_id: entry_gate} for every decision that carries a gate tag."""
    if db_path is None:
        db_path = CLOUD_DB
    conn = sqlite3.connect(db_path)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()]
        if "entry_gate" not in cols:
            conn.close()
            return {}
        rows = conn.execute(
            "SELECT id, entry_gate FROM decisions "
            "WHERE entry_gate IS NOT NULL AND entry_gate != ''"
        ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return {}
    conn.close()
    return {rid: gate for rid, gate in rows}


def build_round_trips(db_path=None):
    """Build equity round-trips, attributing each to the gate of its opening BUY.

    Returns (trips, not_attributed) where each trip carries:
      symbol, open_ts, pnl, pnl_pct, holding_hours, win, gate (or None)
    """
    if db_path is None:
        db_path = CLOUD_DB
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT timestamp, symbol, side, qty, filled_avg_price, status, decision_id "
        "FROM trades WHERE status IN ('filled','partially_filled') ORDER BY id ASC"
    ).fetchall()
    conn.close()
    decision_gates = load_decision_gates(db_path)

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
                                 "gate": decision_gates.get(decision_id)})
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
                gate = b["gate"]
                if not gate:
                    not_attributed += 1
                trips.append({"symbol": symbol, "open_ts": b["ts"], "pnl": pnl,
                              "pnl_pct": pnl_pct, "holding_hours": hold,
                              "win": pnl > 0, "gate": gate})
                tmp -= m
                b["qty"] -= m
                if b["qty"] <= 1e-9:
                    buys[symbol].pop(0)
    return trips, not_attributed


def analyze(db_path=None):
    """Load cloud DB and group round-trips by the gate that opened them."""
    trips, not_attributed = build_round_trips(db_path)
    df = pd.DataFrame(trips)
    if df.empty:
        print("No round-trips found.")
        return

    grouped = df.groupby("gate").agg(
        rt=("pnl", "size"),
        pnl=("pnl", "sum"),
        win=("win", "mean"),
        avg_hold_h=("holding_hours", "mean"),
        largest_win=("pnl", "max"),
        largest_loss=("pnl", "min"),
    ).sort_values("pnl", ascending=False)

    print(f"Total equity round-trips: {len(df)}")
    print(f"Attributed to an entry-gate value: {len(df) - not_attributed}")
    print(f"Not attributed (pre-experiment / untagged): {not_attributed}")
    print()
    print(grouped.to_string())
    print()

    # Per-ticker by gate
    per_ticker = df.groupby(["symbol", "gate"]).agg(
        rt=("pnl", "size"), pnl=("pnl", "sum"), win=("win", "mean")
    ).sort_values("pnl")
    print("Per-ticker by gate:")
    print(per_ticker.to_string())

    # Write report
    lines = [
        "# Equity Entry-Gate A/B Report",
        "",
        f"**Generated:** {datetime.utcnow().isoformat()}Z",
        f"**Source:** `{os.path.basename(CLOUD_DB)}`",
        "",
        f"- Equity round-trips analyzed: **{len(df)}**",
        f"- Attributed to an entry-gate value: **{len(df) - not_attributed}**",
        f"- Not attributed (pre-experiment/untagged): **{not_attributed}**",
        "",
        "## Results by entry-gate value",
        "",
        grouped.to_markdown(),
        "",
        "> Win% is win rate; expectancy = avg net PnL/trade.",
        "## Per-ticker by gate",
        "",
        per_ticker.to_markdown(),
        "",
        "## Caveats",
        "",
        "- Attribution is by the **gate active at entry**, not the gate that exited.",
        "- Small sample is not statistically significant; collect 2-4 weeks.",
        "- 'None' gate = decisions logged before the entry-gate A/B tagging shipped.",
    ]
    out_path = os.path.join(REPORTS_DIR, "entry_gate_ab_report.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\nReport written to {out_path}")


if __name__ == "__main__":
    analyze()