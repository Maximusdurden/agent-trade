from core.blog_stats import calculate_dashboard_stats, round_trips_to_dexter_df
import sqlite3
conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
df = round_trips_to_dexter_df(conn)
conn.close()
stats = calculate_dashboard_stats(df)
print("Dashboard stats:", stats)
