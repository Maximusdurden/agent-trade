import sqlite3
conn = sqlite3.connect(r'Z:\python\projects\agent-trade\cloud_downloaded_trading_agent.db')
cursor = conn.cursor()
print("Tables:", [t[0] for t in cursor.execute('SELECT name FROM sqlite_master WHERE type="table"').fetchall()])
for table in [t[0] for t in cursor.execute('SELECT name FROM sqlite_master WHERE type="table"').fetchall()]:
    try:
        rows = cursor.execute(f'SELECT * FROM {table} ORDER BY rowid DESC LIMIT 3').fetchall()
        print(f"--- Latest in {table} ---")
        for r in rows:
            print(r)
    except Exception as e:
        print(f"Error in {table}: {e}")
conn.close()
