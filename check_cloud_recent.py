import sqlite3
conn = sqlite3.connect(r'Z:\python\projects\agent-trade\cloud_downloaded_trading_agent.db')
cursor = conn.cursor()
for table, query in [
    ('trades', 'SELECT * FROM trades ORDER BY id DESC LIMIT 10'),
    ('decisions', 'SELECT * FROM decisions ORDER BY id DESC LIMIT 10'),
    ('portfolio_history', 'SELECT * FROM portfolio_history ORDER BY id DESC LIMIT 10')
]:
    print(f"=== {table} ===")
    for r in cursor.execute(query).fetchall():
        print(r)
conn.close()
