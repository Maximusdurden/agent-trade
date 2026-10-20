import sqlite3
conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
cursor = conn.cursor()
print("Tables in trading_agent.db:", [t[0] for t in cursor.execute('SELECT name FROM sqlite_master WHERE type="table"').fetchall()])
try:
    print("Realized trades count:", cursor.execute('SELECT COUNT(*) FROM realized_trades').fetchone()[0])
    for r in cursor.execute('SELECT * FROM realized_trades ORDER BY rowid DESC LIMIT 5').fetchall():
        print(r)
except Exception as e:
    print("Error querying realized_trades:", e)
conn.close()
