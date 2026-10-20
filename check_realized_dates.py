import sqlite3
import pandas as pd
conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
realized = pd.read_sql_query('SELECT * FROM realized_trades', conn)
conn.close()
print(realized[['ticker', 'entry_date', 'exit_date', 'pnl_dollar']])
