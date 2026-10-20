import sqlite3
import pandas as pd
from zoneinfo import ZoneInfo
from core import blog_stats

conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
trips = pd.read_sql_query('SELECT * FROM realized_trades', conn).to_dict('records')
conn.close()

df = blog_stats.round_trips_to_dexter_df(trips)
print("df exit_date dtype:", df['exit_date'].dtype if df is not None else None)
print("df exit_date values:", df['exit_date'].dt.tz if df is not None and hasattr(df['exit_date'].dt, 'tz') else "no dt.tz")

target_date = '2026-10-19'
target_ts = pd.Timestamp(pd.to_datetime(target_date), tz="America/New_York")
start = target_ts.normalize()
end = start + pd.Timedelta(days=1)

print("start:", start)
print("end:", end)
print("df rows exit_dates:", df['exit_date'])

day_rows = df[(df["exit_date"] >= start) & (df["exit_date"] < end)]
print("day_rows:", day_rows)
total_pnl = float(day_rows["pnl_dollar"].sum() if not day_rows.empty else 0.0)
print("total_pnl:", total_pnl)
