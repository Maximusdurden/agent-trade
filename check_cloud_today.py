import sqlite3
conn = sqlite3.connect(r'Z:\python\projects\agent-trade\cloud_downloaded_trading_agent.db')
cursor = conn.cursor()
print("--- Trades on 2026-10-19 ---")
for row in cursor.execute('SELECT * FROM trades WHERE timestamp LIKE "2026-10-19%"').fetchall():
    print(row)
print("--- Realized Trades on 2026-10-19 ---")
try:
    for row in cursor.execute('SELECT * FROM realized_trades WHERE timestamp LIKE "2026-10-19%" OR exit_timestamp LIKE "2026-10-19%"').fetchall():
        print(row)
except Exception as e:
    print("realized_trades check error:", e)
conn.close()
