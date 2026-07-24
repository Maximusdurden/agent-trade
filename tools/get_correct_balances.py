import os
import sys
import requests
import json

# Add parent folder to path to ensure root and core package imports work
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import config
import pandas as pd
from datetime import datetime
import pytz

# Fetch live config
api_key = config.ALPACA_API_KEY
secret_key = config.ALPACA_SECRET_KEY
paper = config.ALPACA_PAPER

base_url = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
headers = {
    "APCA-API-KEY-ID": api_key,
    "APCA-API-SECRET-KEY": secret_key
}

eastern = pytz.timezone("America/New_York")

# 1. Fetch current account state
account_url = f"{base_url}/v2/account"
acc_resp = requests.get(account_url, headers=headers)
account_info = acc_resp.json()
current_cash = float(account_info["cash"])
current_equity = float(account_info["equity"])

# 2. Fetch Portfolio History from Alpaca
print("Fetching Portfolio History since 2026-07-01...")
portfolio_url = f"{base_url}/v2/account/portfolio/history"
params = {
    "start": "2026-07-01",
    "timeframe": "1D",
    "intraday_reporting": "market_hours"
}
port_resp = requests.get(portfolio_url, headers=headers, params=params)
port_data = port_resp.json()

port_timestamps = port_data.get("timestamp", [])
port_equity = port_data.get("equity", [])
port_pnl = port_data.get("profit_loss", [])
port_pnl_pct = port_data.get("profit_loss_pct", [])

port_history_rows = []
for i in range(len(port_timestamps)):
    epoch_ts = port_timestamps[i]
    # Convert epoch to Eastern Time date string
    dt_et = datetime.fromtimestamp(epoch_ts, pytz.utc).astimezone(eastern)
    dt_str = dt_et.strftime("%Y-%m-%d")
    
    eq_val = port_equity[i] if i < len(port_equity) else None
    
    # We will filter out dates before 2026-07-01 (like 2026-06-30 which is the base/prior day)
    if dt_str < "2026-07-01":
        continue
        
    port_history_rows.append({
        "date": dt_str,
        "equity": eq_val
    })

df_port = pd.DataFrame(port_history_rows)

# 3. Fetch Activities to reconstruct Cash
print("Fetching all activities since 2026-07-01...")
activities_url = f"{base_url}/v2/account/activities"
all_activities = []
seen_ids = set()
after_ts = "2026-07-01T00:00:00Z"

while True:
    act_params = {
        "after": after_ts,
        "direction": "asc",
        "page_size": 100
    }
    resp = requests.get(activities_url, headers=headers, params=act_params)
    data = resp.json()
    if not data:
        break
    new_items = []
    for item in data:
        item_id = item.get("id")
        if item_id not in seen_ids:
            seen_ids.add(item_id)
            new_items.append(item)
    if not new_items:
        break
    all_activities.extend(new_items)
    if len(data) < 100:
        break
    
    last_item = data[-1]
    last_ts = last_item.get("transaction_time") or last_item.get("date")
    if not last_ts:
        break
    after_ts = last_ts

# Sort activities oldest first
def get_sort_key(act):
    t_time = act.get("transaction_time") or act.get("date") or ""
    return (t_time, act.get("id", ""))
all_activities.sort(key=get_sort_key)

# Calculate the starting cash before any of these activities
total_cash_impact = 0.0
for act in all_activities:
    act_type = act.get("activity_type")
    cash_diff = 0.0
    if act_type == "FILL":
        side = act.get("side")
        qty = float(act.get("qty", 0))
        price = float(act.get("price", 0))
        amount = qty * price
        if side == "buy":
            cash_diff = -amount
        elif side == "sell":
            cash_diff = amount
    elif act_type in ["FEE", "TAX", "TRANS", "DIV", "INT", "JNLC", "JNLS", "CFEE"]:
        net_amount = float(act.get("net_amount", 0))
        cash_diff = net_amount
    else:
        if "net_amount" in act:
            cash_diff = float(act.get("net_amount", 0))
    total_cash_impact += cash_diff

starting_cash = current_cash - total_cash_impact

# Chronologically simulate cash balances
running_cash = starting_cash
daily_cash = {}

# Set initial cash for 2026-07-01
daily_cash["2026-07-01"] = running_cash

for act in all_activities:
    act_type = act.get("activity_type")
    t_time = act.get("transaction_time") or act.get("date") or ""
    
    # Parse date in Eastern Time
    if "T" in t_time:
        # e.g., "2026-07-07T15:48:58.879022Z"
        dt_utc = datetime.strptime(t_time.replace("Z", ""), "%Y-%m-%dT%H:%M:%S.%f" if "." in t_time else "%Y-%m-%dT%H:%M:%S")
        dt_utc = pytz.utc.localize(dt_utc)
        dt_et = dt_utc.astimezone(eastern)
        date_str = dt_et.strftime("%Y-%m-%d")
    else:
        date_str = t_time
        
    cash_diff = 0.0
    if act_type == "FILL":
        side = act.get("side")
        qty = float(act.get("qty", 0))
        price = float(act.get("price", 0))
        amount = qty * price
        if side == "buy":
            cash_diff = -amount
        elif side == "sell":
            cash_diff = amount
    elif act_type in ["FEE", "TAX", "TRANS", "DIV", "INT", "JNLC", "JNLS", "CFEE"]:
        net_amount = float(act.get("net_amount", 0))
        cash_diff = net_amount
    else:
        if "net_amount" in act:
            cash_diff = float(act.get("net_amount", 0))
            
    running_cash += cash_diff
    daily_cash[date_str] = running_cash

# 4. Merge Portfolio History and Cash History
df_report = df_port.copy()

# Add reconstructed cash to the report
cash_list = []
for _, row in df_report.iterrows():
    dt = row["date"]
    # If there are activities on or before this day, get the last known cash balance
    # Let's find the largest date in daily_cash <= dt
    valid_dates = [d for d in daily_cash if d <= dt]
    if valid_dates:
        last_dt = max(valid_dates)
        cash_list.append(daily_cash[last_dt])
    else:
        cash_list.append(starting_cash)

df_report["cash"] = cash_list

# 5. Append live data for today if today's date is not in history
today_str = datetime.now(eastern).strftime("%Y-%m-%d")
if today_str not in df_report["date"].values:
    # Check if there's any newer cash or if we can use current cash
    df_report = pd.concat([df_report, pd.DataFrame([{
        "date": today_str,
        "equity": current_equity,
        "cash": current_cash
    }])], ignore_index=True)

df_report = df_report.sort_values("date").reset_index(drop=True)

# 6. Calculate Holdings, PnL, PnL %
df_report["holdings"] = df_report["equity"] - df_report["cash"]

pnl_usd_list = []
pnl_pct_list = []

# For the first day, let's compare with baseline equity ($69,142.88 from 2026-06-30)
baseline_equity = 69142.88

for idx, row in df_report.iterrows():
    if idx == 0:
        prev_eq = baseline_equity
    else:
        prev_eq = df_report.loc[idx - 1, "equity"]
        
    change = row["equity"] - prev_eq
    change_pct = (change / prev_eq) * 100.0 if prev_eq > 0 else 0.0
    
    pnl_usd_list.append(change)
    pnl_pct_list.append(change_pct)

df_report["dod_pnl_usd"] = pnl_usd_list
df_report["dod_pnl_pct"] = pnl_pct_list

print("\n=== CORRECTED DAY-OVER-DAY BALANCES (America/New_York) ===")
print(df_report.to_string(index=False))

df_report.to_csv("portfolio_dod_balances.csv", index=False)
print("\nSaved corrected report to portfolio_dod_balances.csv")
