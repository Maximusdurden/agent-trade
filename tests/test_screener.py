import sys
import os
import sqlite3
import json
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.database import get_db_connection, log_trade, log_watchlist
from core.screener import get_symbol_win_rates, run_screener
from core.alpaca_client import AlpacaClient
from core.data_provider import DataProvider

def test_screener_and_feedback():
    print("Initializing Screener & Feedback integration test...")
    
    # 1. Inject mock trades to verify FIFO win-rates
    print("Injecting test trades into database...")
    
    # Ensure tables exist
    from core.database import init_db
    init_db()
    
    # Ticker 1: BOOSTED (1 buy at 10, 1 sell at 12 -> +2 Profit = 100% Win Rate)
    log_trade(
        decision_id=9991,
        alpaca_order_id="order_boosted_buy",
        symbol="BOOSTED",
        side="buy",
        qty=10.0,
        filled_avg_price=10.0,
        status="filled"
    )
    log_trade(
        decision_id=9992,
        alpaca_order_id="order_boosted_sell",
        symbol="BOOSTED",
        side="sell",
        qty=10.0,
        filled_avg_price=12.0,
        status="filled"
    )
    
    # Ticker 2: PENALIZED (1 buy at 50, 1 sell at 40 -> -10 Loss = 0% Win Rate)
    log_trade(
        decision_id=9993,
        alpaca_order_id="order_penalized_buy",
        symbol="PENALIZED",
        side="buy",
        qty=5.0,
        filled_avg_price=50.0,
        status="filled"
    )
    log_trade(
        decision_id=9994,
        alpaca_order_id="order_penalized_sell",
        symbol="PENALIZED",
        side="sell",
        qty=5.0,
        filled_avg_price=40.0,
        status="filled"
    )
    
    # Calculate win rates from DB
    win_rates = get_symbol_win_rates()
    print("Calculated Win Rates:", win_rates)
    
    assert "BOOSTED" in win_rates, "BOOSTED ticker missing from win rates"
    assert "PENALIZED" in win_rates, "PENALIZED ticker missing from win rates"
    assert win_rates["BOOSTED"] == 1.0, f"BOOSTED win rate should be 1.0, got: {win_rates['BOOSTED']}"
    assert win_rates["PENALIZED"] == 0.0, f"PENALIZED win rate should be 0.0, got: {win_rates['PENALIZED']}"
    
    print("Database FIFO win-rates calculated successfully.")
    
    # 2. Test watchlists logging to SQLite
    print("Testing watchlist database logging...")
    test_watchlist = ["AAPL", "MSFT", "BOOSTED"]
    watchlist_id = log_watchlist(test_watchlist)
    assert watchlist_id is not None, "Failed to log watchlist"
    
    # Verify DB insertion
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM watchlist_history WHERE id = ?", (watchlist_id,))
        row = cursor.fetchone()
        assert row is not None, "Watchlist history row was not found in DB"
        watchlist_db = json.loads(row["watchlist"])
        assert watchlist_db == test_watchlist, f"Watchlist did not match: got {watchlist_db}"
        
    print(f"Watchlist history table logs verified successfully (Record ID: {watchlist_id}).")
    
    # 3. Test running the complete screener using mock mode
    print("Running complete screener...")
    client = AlpacaClient()
    # Force client is_mock for test predictability
    client.is_mock = True
    provider = DataProvider(client)
    
    selected = run_screener(client, provider, watchlist_limit=3)
    print("Selected Screener Watchlist:", selected)
    
    assert isinstance(selected, list), "Screener did not return a list"
    assert len(selected) <= 3, f"Watchlist length is larger than limit: {len(selected)}"
    
    print("Screener execution test passed successfully.")

if __name__ == "__main__":
    test_screener_and_feedback()
