import sqlite3
conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
cursor = conn.cursor()
print("Filled trades in trades table:")
for r in cursor.execute('SELECT * FROM trades WHERE status="filled" ORDER BY timestamp ASC').fetchall():
    print(r)
conn.close()
