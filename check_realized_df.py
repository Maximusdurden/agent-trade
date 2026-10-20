import sqlite3
import pandas as pd
from core import blog_stats
conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
realized = pd.read_sql_query('SELECT * FROM realized_trades', conn)
conn.close()
print(realized[realized['entry_date'] == '2026-10-19' or realized['exit_date'] == '2026-10-19'])
