import sqlite3
conn = sqlite3.connect('Z:/python/projects/agent-trade/cloud_downloaded_trading_agent.db')
cursor = conn.cursor()
for table in ['trades', 'executions']:
    print(f"=== {table} ===")
    try:
        for r in cursor.execute(f'SELECT * FROM {table} ORDER BY rowid DESC LIMIT 10').fetchall():
            print(r)
    except Exception as e:
        print(e)
conn.close()
