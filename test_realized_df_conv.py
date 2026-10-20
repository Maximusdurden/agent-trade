import sqlite3
import pandas as pd
from core import blog_stats

conn = sqlite3.connect('Z:/python/projects/agent-trade/trading_agent.db')
realized = pd.read_sql_query('SELECT * FROM realized_trades', conn)
conn.close()

# If realized_trades table has rows, convert them directly to dexter df format
rows = []
for idx, r in realized.iterrows():
    rows.append({
        "ticker": r["ticker"],
        "entry_date": pd.to_datetime(r["entry_date"]),
        "exit_date": pd.to_datetime(r["exit_date"]),
        "qty": r["qty"],
        "entry_price": r["entry_price"],
        "exit_price": r["exit_price"],
        "pnl_dollar": r["pnl_dollar"],
        "pnl_percent": r["pnl_percent"],
        "hold_time_minutes": r["hold_time_minutes"],
        "strategy_name": r["strategy_name"]
    })
df = pd.DataFrame(rows)
print("Converted df rows:", len(df))
print(df.tail(2))
