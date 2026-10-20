import sqlite3
conn = sqlite3.connect(r'Z:\python\projects\agent-trade\trading_agent.db')
cursor = conn.cursor()
for table in [t[0] for t in cursor.execute('SELECT name FROM sqlite_master WHERE type="table"').fetchall()]:
    try:
        rows = cursor.execute(f'SELECT * FROM {table} WHERE timestamp LIKE "2026-10-19%"').fetchall()
        if rows:
            print(f"Found in {table}:", len(rows))
    except Exception:
        pass
conn.close()
