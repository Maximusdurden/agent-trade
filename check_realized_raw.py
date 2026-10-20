import sqlite3
import pandas as pd
from core.blog_stats import round_trips_to_dexter_df, calculate_dashboard_stats, get_last_10_days_performance
from core.cards import render_performance_card
from core import wordpress as wp, config
import os

conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
realized = pd.read_sql_query('SELECT * FROM realized_trades', conn)
conn.close()

print("realized exit_date values:", realized['exit_date'].tail(3))
