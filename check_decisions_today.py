import sqlite3
conn = sqlite3.connect(r'Z:\python\projects\agent-trade\trading_agent.db')
cursor = conn.cursor()
for row in cursor.execute('SELECT * FROM decisions WHERE timestamp LIKE "2026-10-19%"').fetchall():
    print(row)
conn.close()
