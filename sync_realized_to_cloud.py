import sqlite3
src = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
dst = sqlite3.connect('Z:/python/projects/agent-trade/cloud_downloaded_trading_agent.db')

# Copy realized_trades and realized_trade_grades if not present in dst
for table in ['realized_trades', 'realized_trade_grades']:
    try:
        dst.execute(f'DROP TABLE IF EXISTS {table}')
        schema = src.execute(f'SELECT sql FROM sqlite_master WHERE type="table" AND name="{table}"').fetchone()[0]
        dst.execute(schema)
        rows = src.execute(f'SELECT * FROM {table}').fetchall()
        placeholders = ','.join(['?'] * len(rows[0])) if rows else ''
        if rows:
            dst.executemany(f'INSERT INTO {table} VALUES ({placeholders})', rows)
        dst.commit()
        print(f"Copied {len(rows)} rows into {table}")
    except Exception as e:
        print(f"Error copying {table}: {e}")

dst.close()
src.close()
