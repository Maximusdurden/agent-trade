#!/usr/bin/env python3
"""Pre/post-change snapshot comparison for agent-trade (TMCL-767).

Captures a snapshot of key trading state before a change, then compares it
against a snapshot taken after the change to detect unexpected drift.

This is the "pre/post-change snapshot comparison" piece of the Validation
Framework epic (TMCL-747). It lets an operator verify that a deploy or code
change did not corrupt trading state.

Usage:
    # Capture a "before" snapshot
    python tools/snapshot_compare.py capture before.json

    # ... make your change / deploy ...

    # Capture an "after" snapshot and compare
    python tools/snapshot_compare.py capture after.json
    python tools/snapshot_compare.py compare before.json after.json

Snapshot captures the following (read-only):
  - Per-table row counts
  - Latest timestamp per time-stamped table
  - Latest watchlist
  - Latest system_state values
  - Latest strategy snapshot per ticker
  - Latest decision per ticker
  - Latest trade per symbol
"""
import argparse
import json
import os
import sqlite3
import sys

TIME_TABLES = ["decisions", "trades", "executions", "portfolio_history",
               "strategy_history", "watchlist_history", "ticker_convictions"]


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def capture(db_path: str) -> dict:
    """Capture a snapshot of trading state."""
    conn = _connect(db_path)
    cur = conn.cursor()
    snap = {"db": db_path}

    # Row counts per table
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    snap["row_counts"] = {}
    for t in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            snap["row_counts"][t] = cur.fetchone()[0]
        except sqlite3.Error:
            snap["row_counts"][t] = None

    # Latest timestamp per time table
    snap["latest_timestamps"] = {}
    for t in TIME_TABLES:
        if t in tables:
            try:
                cur.execute(f"SELECT MAX(timestamp) FROM {t}")
                snap["latest_timestamps"][t] = cur.fetchone()[0]
            except sqlite3.Error:
                snap["latest_timestamps"][t] = None

    # Latest watchlist
    if "watchlist_history" in tables:
        cur.execute("SELECT watchlist FROM watchlist_history ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        snap["latest_watchlist"] = row["watchlist"] if row else None

    # Latest system_state
    if "system_state" in tables:
        cur.execute("SELECT key, value FROM system_state")
        snap["system_state"] = {r["key"]: r["value"] for r in cur.fetchall()}

    # Latest strategy snapshot per ticker
    if "strategy_snapshots" in tables:
        cur.execute("""
            SELECT s.ticker, s.rule, s.instrument_hint, s.strategy_version
            FROM strategy_snapshots s
            JOIN (SELECT ticker, MAX(snapshot_date) AS md FROM strategy_snapshots GROUP BY ticker) m
              ON s.ticker = m.ticker AND s.snapshot_date = m.md
        """)
        snap["latest_strategies"] = {r["ticker"]: dict(r) for r in cur.fetchall()}

    # Latest decision per ticker
    if "decisions" in tables:
        cur.execute("""
            SELECT d.proposed_symbol, d.proposed_action, d.is_approved, d.timestamp
            FROM decisions d
            JOIN (SELECT proposed_symbol, MAX(id) AS mid FROM decisions GROUP BY proposed_symbol) m
              ON d.id = m.mid
        """)
        snap["latest_decisions"] = {r["proposed_symbol"]: dict(r) for r in cur.fetchall()}

    # Latest trade per symbol
    if "trades" in tables:
        cur.execute("""
            SELECT t.symbol, t.side, t.qty, t.status, t.timestamp
            FROM trades t
            JOIN (SELECT symbol, MAX(id) AS mid FROM trades GROUP BY symbol) m
              ON t.id = m.mid
        """)
        snap["latest_trades"] = {r["symbol"]: dict(r) for r in cur.fetchall()}

    conn.close()
    return snap


def compare(before: dict, after: dict) -> list[str]:
    """Compare two snapshots and return a list of differences."""
    diffs = []

    # Row counts
    for table in sorted(set(before.get("row_counts", {})) | set(after.get("row_counts", {}))):
        b = before.get("row_counts", {}).get(table)
        a = after.get("row_counts", {}).get(table)
        if b != a:
            diffs.append(f"row_count[{table}]: {b} -> {a}")

    # Latest timestamps
    for table in sorted(set(before.get("latest_timestamps", {})) | set(after.get("latest_timestamps", {}))):
        b = before.get("latest_timestamps", {}).get(table)
        a = after.get("latest_timestamps", {}).get(table)
        if b != a:
            diffs.append(f"latest_timestamp[{table}]: {b} -> {a}")

    # Watchlist
    if before.get("latest_watchlist") != after.get("latest_watchlist"):
        diffs.append("latest_watchlist changed")

    # system_state
    b_state = before.get("system_state", {})
    a_state = after.get("system_state", {})
    for key in sorted(set(b_state) | set(a_state)):
        if b_state.get(key) != a_state.get(key):
            diffs.append(f"system_state[{key}]: {b_state.get(key)!r} -> {a_state.get(key)!r}")

    # Strategies
    b_strat = before.get("latest_strategies", {})
    a_strat = after.get("latest_strategies", {})
    for ticker in sorted(set(b_strat) | set(a_strat)):
        if b_strat.get(ticker) != a_strat.get(ticker):
            diffs.append(f"strategy[{ticker}] changed")

    # Decisions
    b_dec = before.get("latest_decisions", {})
    a_dec = after.get("latest_decisions", {})
    for sym in sorted(set(b_dec) | set(a_dec)):
        if b_dec.get(sym) != a_dec.get(sym):
            diffs.append(f"decision[{sym}] changed")

    # Trades
    b_tr = before.get("latest_trades", {})
    a_tr = after.get("latest_trades", {})
    for sym in sorted(set(b_tr) | set(a_tr)):
        if b_tr.get(sym) != a_tr.get(sym):
            diffs.append(f"trade[{sym}] changed")

    return diffs


def main():
    parser = argparse.ArgumentParser(description="Pre/post-change snapshot comparison.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    cap = sub.add_parser("capture", help="Capture a snapshot to a JSON file.")
    cap.add_argument("out", help="Output JSON file path")
    cap.add_argument("--db", default="trading_agent.db", help="DB path")

    cmp = sub.add_parser("compare", help="Compare two snapshot JSON files.")
    cmp.add_argument("before", help="Before snapshot JSON")
    cmp.add_argument("after", help="After snapshot JSON")

    args = parser.parse_args()

    if args.cmd == "capture":
        snap = capture(args.db)
        with open(args.out, "w") as f:
            json.dump(snap, f, indent=2, default=str)
        print(f"Captured snapshot to {args.out}")
        return 0

    if args.cmd == "compare":
        with open(args.before) as f:
            before = json.load(f)
        with open(args.after) as f:
            after = json.load(f)
        diffs = compare(before, after)
        if diffs:
            print("Differences found:")
            for d in diffs:
                print(f"  - {d}")
            return 1
        print("No differences found — state is consistent.")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())