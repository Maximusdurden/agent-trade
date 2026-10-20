from core.blog_stats import load_realized_trades_df, calculate_dashboard_stats, get_last_10_days_performance
import sqlite3
conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
df = load_realized_trades_df(conn)
stats = calculate_dashboard_stats(df)
perf = get_last_10_days_performance(conn)
conn.close()
print("Stats:", stats)
print("Perf:", perf)
