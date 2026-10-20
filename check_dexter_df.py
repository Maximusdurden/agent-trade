import sqlite3
from core import blog_stats
conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
df = blog_stats.round_trips_to_dexter_df(conn)
print("Round trips dataframe columns:", df.columns if df is not None else None)
print("Round trips dataframe rows:", df)
conn.close()
