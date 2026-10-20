import sqlite3
import pandas as pd
conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
df = pd.read_sql_query('SELECT * FROM realized_trades', conn)
conn.close()
from core import blog_stats
stats = blog_stats.calculate_dashboard_stats(df)
perf = blog_stats.get_last_10_days_performance(sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db'))
print("Stats:", stats)
print("Perf:", perf)
