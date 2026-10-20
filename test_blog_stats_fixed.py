from core import blog_stats
import sqlite3
conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
df = blog_stats.round_trips_to_dexter_df(conn)
stats = blog_stats.calculate_dashboard_stats(df)
perf = blog_stats.get_last_10_days_performance(conn)
conn.close()
print("Stats:", stats)
print("Perf:", perf)
