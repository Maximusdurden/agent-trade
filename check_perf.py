from core.blog_stats import calculate_dashboard_stats, get_last_10_days_performance
import sqlite3
conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
perf = get_last_10_days_performance(conn)
conn.close()
print("Last 10 days performance:", perf)
