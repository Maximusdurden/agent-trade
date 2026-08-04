"""Generate DoD balances CSV from the DB's portfolio_history directly.
This ensures cash values match what the runner wrote in real-time."""
import sqlite3, csv

db_path = r'Z:\python\projects\agent-trade\trading_agent.db'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT DISTINCT substr(timestamp, 1, 10) as day
    FROM portfolio_history
    ORDER BY day
""").fetchall()

records = []
for row in rows:
    day = row["day"]
    # Get last record of the day
    last = conn.execute("""
        SELECT equity, cash, unrealized_pnl FROM portfolio_history
        WHERE substr(timestamp, 1, 10) = ?
        ORDER BY timestamp DESC LIMIT 1
    """, (day,)).fetchone()
    if not last:
        continue
    equity = round(last["equity"], 2)
    cash = round(last["cash"], 2)
    holdings = round(equity - cash, 2)
    records.append({"date": day, "equity": equity, "cash": cash, "holdings": holdings})

# Calculate DoD PnL
prev_equity = None
for rec in records:
    if prev_equity is None:
        rec["dod_pnl_usd"] = 0.0
        rec["dod_pnl_pct"] = 0.0
    else:
        chg = round(rec["equity"] - prev_equity, 2)
        pct = round((chg / prev_equity) * 100, 4) if prev_equity > 0 else 0.0
        rec["dod_pnl_usd"] = chg
        rec["dod_pnl_pct"] = pct
    prev_equity = rec["equity"]

# Write CSV
csv_path = r'Z:\python\projects\agent-trade\portfolio_dod_balances.csv'
with open(csv_path, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["date","equity","cash","holdings","dod_pnl_usd","dod_pnl_pct"])
    w.writeheader()
    w.writerows(records)

print(f"Written {len(records)} records to {csv_path}")
print(f"Date range: {records[0]['date']} -> {records[-1]['date']}")
print(f"First: ${records[0]['equity']} | Last: ${records[-1]['equity']}")
print()
print(f"{'Date':<12} {'Equity':>10} {'Cash':>10} {'Holdings':>10} {'DoD PnL':>10} {'DoD %':>8}")
print("-" * 62)
for r in records:
    print(f"{r['date']:<12} ${r['equity']:>8.2f} ${r['cash']:>8.2f} ${r['holdings']:>8.2f} ${r['dod_pnl_usd']:>+8.2f} {r['dod_pnl_pct']:>+7.2f}%")

conn.close()