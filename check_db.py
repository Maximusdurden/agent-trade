import sqlite3
conn = sqlite3.connect('z:/python/projects/agent-trade/trading_agent.db')
c = conn.cursor()
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in c.fetchall()]
print("Tables:", tables)

# Check heartbeat table
for tbl in tables:
    if 'heartbeat' in tbl.lower():
        c.execute(f"SELECT * FROM {tbl} ORDER BY rowid DESC LIMIT 3")
        cols = [d[0] for d in c.description]
        print(f"\nHeartbeat table: {tbl}")
        print("Columns:", cols)
        for row in c.fetchall():
            print(dict(zip(cols, row)))

# Check decisions
c.execute("SELECT timestamp, proposed_action, proposed_symbol, thought_process FROM decisions ORDER BY timestamp DESC LIMIT 3")
print("\nLatest decisions:")
for r in c.fetchall():
    print(dict(r))

conn.close()