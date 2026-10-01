#!/usr/bin/env python3
"""P3 analysis: how often do swing RSI-2 signals fire at different thresholds?

Fetches daily bars for the swing universe + flatbase universe, computes RSI_2,
and counts how many days in the last 12 months would have triggered at
RSI < 10 (current), < 12, < 15. Also checks flatbase breakout frequency at
different RVOL thresholds. Read-only.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

import pandas as pd
import numpy as np

client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, raw_data=True)

SWING_UNIVERSE = ["AMD", "NVDA", "TSLA", "SMH", "MSFT", "AAPL", "GOOGL", "META"]
FLATBASE_UNIVERSE = [
    "NVDA", "AVGO", "DELL", "HPE", "MU", "META", "TSLA", "SMH", "QQQ",
    "AMZN", "GOOGL", "MSFT", "NFLX", "APP", "ANET", "CRWD", "NOW", "PANW",
    "COIN", "MSTR", "HOOD", "SHOP", "UBER", "VRT", "CAT", "GLD", "GE",
]

start = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


def _rsi2(close: pd.Series) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / 2, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / 2, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return (100.0 - (100.0 / (1.0 + rs))).fillna(50.0)


def fetch_daily(symbol: str) -> pd.DataFrame:
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end)
    bars = client.get_stock_bars(req)
    if symbol not in bars:
        return pd.DataFrame()
    rows = []
    for b in bars[symbol]:
        rows.append({"date": str(b["t"])[:10], "close": float(b["c"]), "high": float(b["h"]),
                     "low": float(b["l"]), "open": float(b["o"]), "volume": float(b["v"])})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()


print("=" * 80)
print("SWING RSI-2 signal frequency (last 12 months)")
print("=" * 80)
print(f"{'sym':>6} {'days':>5} {'RSI<10':>7} {'RSI<12':>7} {'RSI<15':>7} {'RSI<20':>7}")
for sym in SWING_UNIVERSE:
    df = fetch_daily(sym)
    if df.empty or len(df) < 250:
        print(f"{sym:>6}  no data")
        continue
    df = df.iloc[-252:]
    rsi = _rsi2(df["close"])
    sma200 = df["close"].rolling(200).mean()
    # Macro gate: close > SMA200 (shifted, t-1)
    macro = df["close"].shift(1) > sma200.shift(1)
    for thresh in (10, 12, 15, 20):
        sig = (rsi.shift(1) < thresh) & macro
        n = int(sig.sum())
        if thresh == 10:
            n10 = n
        elif thresh == 12:
            n12 = n
        elif thresh == 15:
            n15 = n
        else:
            n20 = n
    print(f"{sym:>6} {len(df):>5} {n10:>7} {n12:>7} {n15:>7} {n20:>7}")

print()
print("=" * 80)
print("FLATBASE breakout frequency proxy (last 12 months)")
print("=" * 80)
print("(count of days where close[t-1] > 20d high AND vol > rvol*vol20)")
print(f"{'sym':>6} {'days':>5} {'rvol1.5':>8} {'rvol1.2':>8} {'rvol1.0':>8}")
for sym in FLATBASE_UNIVERSE:
    df = fetch_daily(sym)
    if df.empty or len(df) < 250:
        print(f"{sym:>6}  no data")
        continue
    df = df.iloc[-252:]
    base_high = df["high"].rolling(20).max().shift(1)
    vol20 = df["volume"].rolling(20).mean().shift(1)
    breakout = df["close"].shift(1) > base_high.shift(1)
    for rvol in (1.5, 1.2, 1.0):
        sig = breakout & (df["volume"].shift(1) >= rvol * vol20.shift(1))
        n = int(sig.sum())
        if rvol == 1.5:
            n15 = n
        elif rvol == 1.2:
            n12 = n
        else:
            n10 = n
    print(f"{sym:>6} {len(df):>5} {n15:>8} {n12:>8} {n10:>8}")