#!/usr/bin/env python3
"""Fetch index/benchmark data for the last 2 weeks to compare market context."""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, raw_data=True)

symbols = ["SPY", "QQQ", "IWM", "DIA", "MSFT", "AMD", "NVDA", "TSLA", "META"]
start = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

req = StockBarsRequest(
    symbol_or_symbols=symbols,
    timeframe=TimeFrame.Day,
    start=start,
    end=end,
)
bars = client.get_stock_bars(req)

print(f"Daily bars {start} -> {end}")
print("=" * 90)
for sym in symbols:
    if sym not in bars:
        print(f"{sym}: NO DATA")
        continue
    df = bars[sym]
    rows = []
    for bar in df:
        ts = bar.get("t") or bar.get("timestamp")
        close = bar.get("c") or bar.get("close")
        rows.append((str(ts)[:10], round(float(close), 2)))
    if not rows:
        print(f"{sym}: no rows")
        continue
    first_close = rows[0][1]
    last_close = rows[-1][1]
    chg = (last_close / first_close - 1) * 100
    print(f"\n{sym}: {first_close} -> {last_close} ({chg:+.2f}%)")
    for d, c in rows[-6:]:
        print(f"  {d}: {c}")