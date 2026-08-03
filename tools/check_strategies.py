"""Check strategy rules for crypto symbols."""
import sys, os, sqlite3

sys.path.insert(0, "Z:/python/projects/agent-trade")
os.chdir("Z:/python/projects/agent-trade")

conn = sqlite3.connect("trading_agent.db")
cur = conn.cursor()

# Check strategy_history for SOL
cur.execute("SELECT * FROM strategy_history WHERE ticker LIKE ? ORDER BY id DESC LIMIT 5", ("%SOL%",))
rows = cur.fetchall()
cur.execute("PRAGMA table_info(strategy_history)")
cols = [c[1] for c in cur.fetchall()]
print("Columns:", cols)
for r in rows:
    print(r)

print()
# Check strategy_history for BTC
cur.execute("SELECT * FROM strategy_history WHERE ticker LIKE ? ORDER BY id DESC LIMIT 5", ("%BTC%",))
for r in cur.fetchall():
    print(r)

print()
# Check if there's a strategies table or active_strategy in system_state
cur.execute("SELECT * FROM system_state")
for r in cur.fetchall():
    print(r)
    
print()
# Check for any table with 'strategy' in the name
cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%strategy%'")
tables = [r[0] for r in cur.fetchall()]
print("Strategy-related tables:", tables)

# Check all strategy_history tickers
cur.execute("SELECT DISTINCT ticker FROM strategy_history")
tickers = [r[0] for r in cur.fetchall()]
print("Tickers with strategy rules:", tickers)

conn.close()