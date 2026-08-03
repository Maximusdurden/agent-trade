"""Test crypto data fetching directly."""
import sys
import os
sys.path.insert(0, "Z:/python/projects/agent-trade")
os.chdir("Z:/python/projects/agent-trade")

import logging
import pandas as pd
logging.basicConfig(level=logging.INFO, format="%(message)s")

from core.alpaca_client import AlpacaClient
from core.data_provider import DataProvider

client = AlpacaClient()
print(f"Mock mode: {client.is_mock}")

# Test 1: Get 5min bars for SOL/USD using the raw method
print("\n=== Test 1: Raw get_historical_bars for SOL/USD (5min) ===")
df = client.get_historical_bars("SOL/USD", limit=100, timeframe_str="5min")
if df.empty:
    print("EMPTY DATAFRAME")
else:
    print(f"Shape: {df.shape}")
    print(f"Columns: {list(df.columns)}")
    print(f"Index type: {type(df.index)}")
    if isinstance(df.index, pd.MultiIndex):
        print(f"Index levels: {df.index.levels[0] if hasattr(df.index, 'levels') else 'N/A'}")
    print(f"First few rows:")
    print(df.head(3))
    print(f"Last few rows:")
    print(df.tail(3))

# Test 2: Get daily bars for SOL/USD
print("\n=== Test 2: Raw get_historical_bars for SOL/USD (day) ===")
df2 = client.get_historical_bars("SOL/USD", limit=35, timeframe_str="day")
if df2.empty:
    print("EMPTY DATAFRAME")
else:
    print(f"Shape: {df2.shape}")
    print(df2.tail(3))

# Test 3: Use data_provider
print("\n=== Test 3: DataProvider.get_market_state for SOL/USD ===")
provider = DataProvider(client)
state = provider.get_market_state("SOL")
if state:
    print(f"Price: {state.get('current_price')}")
    print(f"Daily return: {state.get('daily_return_pct')}")
    print(f"RSI: {state.get('indicators', {}).get('rsi_14')}")
else:
    print("EMPTY STATE (returned {})")

# Test 4: Try BTC
print("\n=== Test 4: DataProvider.get_market_state for BTC ===")
state2 = provider.get_market_state("BTC")
if state2:
    print(f"Price: {state2.get('current_price')}")
    print(f"Daily return: {state2.get('daily_return_pct')}")
    print(f"RSI: {state2.get('indicators', {}).get('rsi_14')}")
else:
    print("EMPTY STATE (returned {})")