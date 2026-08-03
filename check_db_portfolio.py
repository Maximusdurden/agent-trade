import sqlite3
import os

db = os.path.join(os.environ['TEMP'], 'check_portfolio.db')
conn = sqlite3.connect(db)
c = conn.cursor()

# Check tables
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in c.fetchall()]
print('Tables:', tables)

# Check portfolio_history
if 'portfolio_history' in tables:
    c.execute('SELECT COUNT(*) FROM portfolio_history')
    count = c.fetchone()[0]
    print(f'Portfolio history rows: {count}')
    if count > 0:
        c.execute('SELECT * FROM portfolio_history ORDER BY timestamp DESC LIMIT 5')
        for r in c.fetchall():
            print(f'  ts={r[0]}, eq={r[1]}, cash={r[2]}, pnl={r[3]}')
    else:
        print('Empty!')

# Check decisions
if 'decisions' in tables:
    c.execute('SELECT COUNT(*) FROM decisions')
    print(f'Decisions rows: {c.fetchone()[0]}')

# Check trades
if 'trades' in tables:
    c.execute('SELECT COUNT(*) FROM trades')
    print(f'Trades rows: {c.fetchone()[0]}')
    c.execute("SELECT alpaca_order_id FROM trades WHERE alpaca_order_id LIKE 'mock-order-%'")
    mock_left = len(c.fetchall())
    print(f'Mock orders remaining: {mock_left}')

conn.close()