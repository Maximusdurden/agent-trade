import sys
import os
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.alpaca_client import AlpacaClient
from core.data_provider import DataProvider

def test_batch_and_vectorization():
    print("Initializing components...")
    client = AlpacaClient()
    provider = DataProvider(client)
    
    # Try fetching a small batch of stock tickers
    symbols = ["AAPL", "MSFT", "SPY"]
    print(f"Fetching historical daily bars for {symbols}...")
    df = client.get_historical_bars(symbols, limit=30, timeframe_str="day")
    
    assert not df.empty, "Returned DataFrame is empty"
    assert isinstance(df.index, pd.MultiIndex), "Index is not a MultiIndex"
    assert df.index.names == ["symbol", "timestamp"], f"Index names are incorrect: {df.index.names}"
    
    print("MultiIndex DataFrame verified successfully.")
    print("Shape:", df.shape)
    
    # Calculate indicators
    print("Calculating technical indicators...")
    df_with_inds = provider._add_technical_indicators(df)
    
    # Validate indicators exist
    required_cols = [
        "rsi_14", "sma_20", "sma_50", "macd_line", "macd_signal", "macd_hist",
        "bollinger_upper", "bollinger_lower"
    ]
    for col in required_cols:
        assert col in df_with_inds.columns, f"Indicator column {col} missing from output"
        
    print("All standard technical indicator columns are present.")
    
    # Check that there is no data bleed between symbols.
    # If there is data bleed, indicator calculation (e.g., SMA) might cross symbol boundaries.
    # Let's verify by computing SMA manually on one symbol and comparing
    for sym in symbols:
        sym_slice = df_with_inds.loc[sym]
        close_series = sym_slice["close"]
        manual_sma_20 = close_series.rolling(20).mean()
        
        # Select rows where manual SMA is valid (not NaN)
        valid_idx = manual_sma_20.dropna().index
        for idx in valid_idx[:5]:
            calc_val = sym_slice.loc[idx, "sma_20"]
            man_val = manual_sma_20.loc[idx]
            assert np.isclose(calc_val, man_val, rtol=1e-5), f"Data bleed detected for {sym} at {idx}: calculated {calc_val} vs manual {man_val}"
            
    print("Symbol-isolated indicator calculations validated. No bleed detected!")
    print("Batch and Vectorization test passed successfully.")

if __name__ == "__main__":
    test_batch_and_vectorization()
