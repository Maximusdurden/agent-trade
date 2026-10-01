#!/usr/bin/env python3
"""Analyze the cloud trading DB: schema, recent activity, trades, decisions."""
import sqlite3
import sys
from datetime import datetime, timedelta

DB = sys.argv[1] if len(sys.argv) > 1 else "cloud_downloaded_trading_agent.db"

conn = sqlite3.connect(DB)
cur = conn.cursor()

print("=" * 80)
print(f"DB: {DB}")
print("=" * 80)

tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("\nTABLES:")
for t in tables:
    try:
        n = cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        print(f"  {t}: {n} rows")
    except Exception as e:
        print(f"  {t}: ERR {e}")

# Check key tables' schemas
for t in ["trades", "realized_trades", "decisions", "portfolio_history", "portfolio_state", "executions", "orders"]:
    if t in tables:
        cols = [r[1] for r in cur.execute(f'PRAGMA table_info("{t}")')]
        print(f"\n{t} columns: {cols}")

conn.close()