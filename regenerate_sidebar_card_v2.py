import sqlite3
import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from core.blog_stats import round_trips_to_dexter_df, calculate_dashboard_stats, get_last_10_days_performance
from core.cards import render_performance_card
from core import wordpress as wp, config
import os

conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
realized = pd.read_sql_query('SELECT * FROM realized_trades', conn)
conn.close()

trips = []
for idx, r in realized.iterrows():
    trips.append({
        "symbol": r["ticker"],
        "open_ts": str(r["entry_date"]),
        "close_ts": str(r["exit_date"]),
        "qty": r["qty"],
        "entry_price": r["entry_price"],
        "exit_price": r["exit_price"],
        "pnl": r["pnl_dollar"],
        "pnl_pct": r["pnl_percent"],
        "holding_hours": (r["hold_time_minutes"] or 0.0) / 60.0,
        "strategy_name": r["strategy_name"]
    })

df = round_trips_to_dexter_df(trips)
# Filter directly by string match on exit_date or pd.to_datetime comparison
df['exit_date_str'] = df['exit_date'].astype(str).str[:10]
target_date = "2026-10-19"
day_rows = df[df['exit_date_str'] == target_date]
total_pnl = float(day_rows["pnl_dollar"].sum() if not day_rows.empty else 0.0)
print("Computed total_pnl for sidebar card (string match):", total_pnl)
print("day_rows:", day_rows)

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
