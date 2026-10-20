import sqlite3
import pandas as pd
from core import blog_stats

conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
df = pd.read_sql_query('SELECT * FROM realized_trades', conn)
conn.close()

stats = blog_stats.calculate_dashboard_stats(df)
perf = blog_stats.get_last_10_days_performance(df)
print("Stats:", stats)
print("Perf for today (2026-10-19):", perf.get('2026-10-19'))
print("All Perf:", perf)
