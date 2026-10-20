import sqlite3
conn = sqlite3.connect('Z:/python/projects/agent-trade/cloud_downloaded_trading_agent.db')
cursor = conn.cursor()
for table in ['decisions', 'trades', 'executions', 'portfolio_history']:
    try:
        cols = [col[1] for col in cursor.execute(f'PRAGMA table_info({table})').fetchall()]
        print(table, cols)
        rows = cursor.execute(f'SELECT * FROM {table} WHERE timestamp LIKE "2026-10-19%"').fetchall()
        print(f"Rows for 2026-10-19 in {table}:", len(rows))
        for r in rows[:5]:
            print("  ", r)
    except Exception as e:
        print(table, "error:", e)
conn.close()
