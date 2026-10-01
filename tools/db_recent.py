#!/usr/bin/env python3
"""Analyze recent activity in the cloud trading DB."""
import sqlite3
import sys
from datetime import datetime, timedelta

DB = sys.argv[1] if len(sys.argv) > 1 else "cloud_downloaded_trading_agent.db"
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 14

conn = sqlite3.connect(DB)
cur = conn.cursor()

cutoff = (datetime.utcnow() - timedelta(days=DAYS)).strftime("%Y-%m-%d")
print(f"=== Recent activity since {cutoff} (UTC) ===")

print("\n--- TRADES (last 30) ---")
try:
    rows = cur.execute("""
        SELECT timestamp, symbol, side, qty, filled_avg_price, status, option_type, option_dte, strike
        FROM trades WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT 30
    """, (cutoff,)).fetchall()
    for r in rows:
        print(r)
    if not rows:
        print("(none)")
except Exception as e:
    print(f"ERR: {e}")

print("\n--- DECISIONS by day (last 14 days) ---")
try:
    rows = cur.execute("""
        SELECT substr(timestamp,1,10) as day, COUNT(*) as n,
               SUM(CASE WHEN proposed_action='BUY' THEN 1 ELSE 0 END) as buys,
               SUM(CASE WHEN proposed_action='SELL' THEN 1 ELSE 0 END) as sells,
               SUM(CASE WHEN is_approved=1 THEN 1 ELSE 0 END) as approved
        FROM decisions WHERE timestamp >= ? GROUP BY day ORDER BY day DESC
    """, (cutoff,)).fetchall()
    for r in rows:
        print(r)
except Exception as e:
    print(f"ERR: {e}")

print("\n--- DECISIONS last 20 ---")
try:
    rows = cur.execute("""
        SELECT timestamp, proposed_symbol, proposed_action, proposed_qty, is_approved, rejection_reason, direction, conviction, instrument, model, entry_gate
        FROM decisions ORDER BY timestamp DESC LIMIT 20
    """).fetchall()
    for r in rows:
        print(r)
except Exception as e:
    print(f"ERR: {e}")

print("\n--- PORTFOLIO HISTORY (all) ---")
try:
    rows = cur.execute("SELECT timestamp, equity, cash, unrealized_pnl FROM portfolio_history ORDER BY timestamp").fetchall()
    for r in rows:
        print(r)
except Exception as e:
    print(f"ERR: {e}")

print("\n--- EXECUTIONS (all) ---")
try:
    rows = cur.execute("SELECT * FROM executions ORDER BY timestamp DESC LIMIT 30").fetchall()
    for r in rows:
        print(r)
except Exception as e:
    print(f"ERR: {e}")

print("\n--- SYSTEM STATE (last 20) ---")
try:
    rows = cur.execute("SELECT * FROM system_state ORDER BY rowid DESC LIMIT 20").fetchall()
    for r in rows:
        print(r)
except Exception as e:
    print(f"ERR: {e}")

conn.close()