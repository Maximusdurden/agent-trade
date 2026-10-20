import sqlite3
conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
cursor = conn.cursor()
print("Realized trades for 2026-10-19:")
for r in cursor.execute('SELECT * FROM realized_trades WHERE entry_date="2026-10-19" OR exit_date="2026-10-19"').fetchall():
    print(r)
conn.close()
