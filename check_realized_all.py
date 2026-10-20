import sqlite3
conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
cursor = conn.cursor()
print("realized_trades rows:")
for r in cursor.execute('SELECT * FROM realized_trades').fetchall():
    print(r)
conn.close()
