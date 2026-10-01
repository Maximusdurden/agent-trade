#!/usr/bin/env python3
"""Check MSFT and recent trades/decisions in the cloud DB."""
import sqlite3

conn = sqlite3.connect("cloud_downloaded_trading_agent.db")
cur = conn.cursor()

print("MSFT trades:")
for r in cur.execute("SELECT timestamp,symbol,side,qty,filled_avg_price,status FROM trades WHERE symbol='MSFT'"):
    print(" ", r)

print("\nMSFT decisions:")
for r in cur.execute("SELECT timestamp,proposed_symbol,proposed_action,proposed_qty,is_approved,rejection_reason,cycle_id FROM decisions WHERE proposed_symbol='MSFT' ORDER BY timestamp DESC LIMIT 10"):
    print(" ", r)

print("\nAll trades since 9/22:")
for r in cur.execute("SELECT timestamp,symbol,side,qty,filled_avg_price,status FROM trades WHERE timestamp >= '2026-09-22' ORDER BY timestamp"):
    print(" ", r)

print("\nDistinct cycle_ids in decisions (last 30):")
for r in cur.execute("SELECT DISTINCT cycle_id FROM decisions ORDER BY cycle_id DESC LIMIT 30"):
    print(" ", r)

conn.close()