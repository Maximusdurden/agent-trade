"""Quick DB diagnostics for dashboard decision stream."""
import sqlite3
import sys

db_path = "Z:/python/projects/agent-trade/trading_agent.db"
conn = sqlite3.connect(db_path)
cur = conn.cursor()

# Show all tables
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cur.fetchall()]
print("TABLES:", tables)

# Look for decisions table or similar
for table in tables:
    cur.execute(f"PRAGMA table_info(\"{table}\")")
    cols = [c[1] for c in cur.fetchall()]
    print(f"\n=== {table} (columns: {cols}) ===")
    cur.execute(f"SELECT * FROM \"{table}\" ORDER BY rowid DESC LIMIT 3")
    rows = cur.fetchall()
    for r in rows:
        print(f"  {r}")
    cur.execute(f"SELECT COUNT(*) FROM \"{table}\"")
    count = cur.fetchone()[0]
    print(f"  Total rows: {count}")
    # Max timestamp
    ts_cols = ['timestamp', 'created_at', 'time', 'last_heartbeat', 'completed_at', 'updated_at']
    for ts_col in ts_cols:
        if ts_col in cols:
            cur.execute(f"SELECT MAX(\"{ts_col}\") FROM \"{table}\"")
            max_ts = cur.fetchone()[0]
            if max_ts:
                print(f"  Max {ts_col}: {max_ts}")
                break

conn.close()