import sqlite3
import pandas as pd
from core import blog_stats

conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
trips = pd.read_sql_query('SELECT * FROM realized_trades', conn).to_dict('records')
conn.close()

df = blog_stats.round_trips_to_dexter_df(trips)
print("df exit_date:", df['exit_date'])
print("df exit_date type:", type(df['exit_date'].iloc[0]))
