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
EASTER_TZ = "America/New_York"
if not df.empty and not isinstance(df["exit_date"].iloc[0], pd.Timestamp):
    df["exit_date"] = pd.to_datetime(df["exit_date"])
# Make exit_date tz-naive matching local date comparisons or explicit tz
df["exit_date"] = df["exit_date"].dt.tz_localize(None)

target_date = "2026-10-19"
target_ts = pd.Timestamp(pd.to_datetime(target_date))
start = target_ts.normalize()
end = start + pd.Timedelta(days=1)

if not df.empty:
    day_rows = df[(df["exit_date"] >= start) & (df["exit_date"] < end)]
else:
    day_rows = pd.DataFrame()
total_pnl = float(day_rows["pnl_dollar"].sum() if not day_rows.empty else 0.0)
print("Computed total_pnl for sidebar card (naive):", total_pnl)
print("day_rows:", day_rows)

stats = calculate_dashboard_stats(df, today=start.date())
streak = get_last_10_days_performance(df, today=start.date())
card_path = os.path.join("reports", f"daily_card_{target_date}.png")
render_performance_card(stats, streak, total_pnl, card_path, title_date=target_date)

media = wp.upload_image_from_path(card_path, f"Daily Summary {target_date}")
if media:
    print("Uploaded media source URL:", media.get("source_url"))
    wp.update_home_template_sidebar(media.get("source_url"))
    wp.update_synced_pattern(media.get("source_url"))
    print("Updated home template sidebar and synced pattern with correct PnL card.")
