import sqlite3
conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
cursor = conn.cursor()
print("Trades on 2026-10-19:")
for r in cursor.execute('SELECT * FROM trades WHERE timestamp LIKE "2026-10-19%"').fetchall():
    print(r)
conn.close()
