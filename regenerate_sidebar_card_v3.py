import sqlite3
import pandas as pd
from core.blog_stats import calculate_dashboard_stats, get_last_10_days_performance
from core.cards import render_performance_card
from core import wordpress as wp
import os

conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
df = pd.read_sql_query('SELECT * FROM realized_trades', conn)
conn.close()

# Ensure datetime types
df['exit_date'] = pd.to_datetime(df['exit_date'])
df['entry_date'] = pd.to_datetime(df['entry_date'])

target_date = "2026-10-19"
day_rows = df[df['exit_date'].astype(str).str.startswith(target_date)]
total_pnl = float(day_rows['pnl_dollar'].sum() if not day_rows.empty else 0.0)
print("Correct total_pnl for sidebar card:", total_pnl)

stats = calculate_dashboard_stats(df)
streak = get_last_10_days_performance(df)
card_path = os.path.join("reports", f"daily_card_{target_date}.png")
render_performance_card(stats, streak, total_pnl, card_path, title_date=target_date)

media = wp.upload_image_from_path(card_path, f"Daily Summary {target_date}")
if media:
    print("Uploaded media source URL:", media.get("source_url"))
    wp.update_home_template_sidebar(media.get("source_url"))
    wp.update_synced_pattern(media.get("source_url"))
    print("Updated home template sidebar and synced pattern with correct PnL card.")
