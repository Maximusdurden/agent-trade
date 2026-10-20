import sqlite3
conn = sqlite3.connect(r'Z:\python\projects\agent-trade\trading_agent.db')
cursor = conn.cursor()
rows = cursor.execute('SELECT * FROM trades WHERE timestamp LIKE "2026-10-19%"').fetchall()
print("Trades on 2026-10-19:", rows)
conn.close()
