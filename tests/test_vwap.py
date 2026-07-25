import sys
import os
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.alpaca_client import AlpacaClient
from core.data_provider import DataProvider

def test_vwap_reset_behavior():
    print("Initializing test for intraday-resetting VWAP...")
    
    # 1. Create a synthetic DataFrame of 15-minute bars for a single ticker spanning 2 days
    # Day 1: 2026-07-24 (9:30 to 16:00 ET)
    # Day 2: 2026-07-25 (9:30 to 16:00 ET)
    
    times_day1 = pd.date_range(start="2026-07-24 09:30:00-04:00", end="2026-07-24 16:00:00-04:00", freq="15min")
    times_day2 = pd.date_range(start="2026-07-25 09:30:00-04:00", end="2026-07-25 16:00:00-04:00", freq="15min")
    all_times = times_day1.union(times_day2)
    
    # Prices on Day 1: start at 100, trend up to 127
    # Prices on Day 2: start down at 105 (overnight gap), trend up to 132
    prices = np.concatenate([
        np.linspace(100, 127, len(times_day1)),
        np.linspace(105, 132, len(times_day2))
    ])
    
    # Volumes: 1000 shares per bar
    volumes = np.full(len(all_times), 1000.0)
    
    data = {
        "open": prices,
        "high": prices + 0.5,
        "low": prices - 0.5,
        "close": prices,
        "volume": volumes
    }
    
    # Single-index DataFrame (represented as MultiIndex for generic support)
    index = pd.MultiIndex.from_product([["TEST_TICKER"], all_times], names=["symbol", "timestamp"])
    df = pd.DataFrame(data, index=index)
    
    # Use DataProvider to calculate technical indicators
    client = AlpacaClient()
    provider = DataProvider(client)
    
    print("Calculating indicators...")
    df_result = provider._add_technical_indicators(df)
    
    # Verify columns
    vwap_cols = ["vwap", "vwap_upper_1", "vwap_lower_1", "vwap_upper_2", "vwap_lower_2", "vwap_dist_pct"]
    for col in vwap_cols:
        assert col in df_result.columns, f"VWAP column {col} missing from results"
        
    print("VWAP indicator columns verified.")
    
    # Check first bar of Day 1
    # Typical price for first bar = (100 + 100.5 + 99.5) / 3 = 100.0
    first_bar_day1 = df_result.loc[("TEST_TICKER", times_day1[0])]
    assert np.isclose(first_bar_day1["vwap"], 100.0), f"Day 1 first bar VWAP is not close to typical price: {first_bar_day1['vwap']}"
    
    # Check first bar of Day 2
    # Typical price for first bar of Day 2 = (105 + 105.5 + 104.5) / 3 = 105.0
    # If resetting works correctly, the VWAP for the first bar of Day 2 should be exactly 105.0, 
    # regardless of Day 1's prices or cumulative sums.
    first_bar_day2 = df_result.loc[("TEST_TICKER", times_day2[0])]
    assert np.isclose(first_bar_day2["vwap"], 105.0), f"Day 2 first bar VWAP did not reset! Got: {first_bar_day2['vwap']}, expected 105.0"
    
    print("VWAP successfully reset to opening typical price at the start of Day 2!")
    
    # Verify standard deviation bands and vwap_dist_pct
    for idx, row in df_result.iterrows():
        close = row["close"]
        vwap = row["vwap"]
        vwap_upper_1 = row["vwap_upper_1"]
        vwap_lower_1 = row["vwap_lower_1"]
        vwap_dist_pct = row["vwap_dist_pct"]
        
        # Verify band bounds
        assert vwap_upper_1 >= vwap, "VWAP Upper Band 1 is below VWAP"
        assert vwap_lower_1 <= vwap, "VWAP Lower Band 1 is above VWAP"
        
        # Verify distance percentage formula
        expected_dist = ((close - vwap) / vwap) * 100
        assert np.isclose(vwap_dist_pct, expected_dist, rtol=1e-5), f"vwap_dist_pct is incorrect: calculated {vwap_dist_pct} vs expected {expected_dist}"
        
    print("VWAP standard deviation bands and distance percentages verified.")
    print("VWAP reset behavioral test passed successfully.")

if __name__ == "__main__":
    test_vwap_reset_behavior()
