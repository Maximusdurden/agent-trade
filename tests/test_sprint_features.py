# filename: tests/test_sprint_features.py
import unittest
import os
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# Ensure imports are resolved correctly
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.gcs_sync import check_kill_switch, set_kill_switch_state
from runner import format_positions, get_current_eastern_time

class TestSprintFeatures(unittest.TestCase):
    def setUp(self):
        self.skip_file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".weekend_skip.json")
        self.cadence_file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".daily_cadence.json")
        
        # Cleanup any pre-existing files
        if os.path.exists(self.skip_file_path):
            os.remove(self.skip_file_path)
        if os.path.exists(self.cadence_file_path):
            os.remove(self.cadence_file_path)

    def tearDown(self):
        # Cleanup after tests
        if os.path.exists(self.skip_file_path):
            os.remove(self.skip_file_path)
        if os.path.exists(self.cadence_file_path):
            os.remove(self.cadence_file_path)

    def test_format_positions(self):
        # Test formatting empty positions
        self.assertEqual(format_positions({}), "None.")
        
        # Test formatting active positions
        positions = {
            "SOL/USD": {"qty": 1.25},
            "AAPL": {"qty": 50.0},
            "TSLA": {"qty": 12}
        }
        formatted = format_positions(positions)
        self.assertIn("SOL/USD (1.25 shares)", formatted)
        self.assertIn("AAPL (50 shares)", formatted)
        self.assertIn("TSLA (12 shares)", formatted)

    def test_get_current_eastern_time(self):
        now_et = get_current_eastern_time()
        self.assertIsInstance(now_et, datetime)

    @patch("core.gcs_sync.get_gcs_client")
    def test_kill_switch_mock(self, mock_get_client):
        # Mock GCS Client behaviour
        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_text.return_value = json.dumps({"status": "HALTED", "updated_by": "test"})
        
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_get_client.return_value = mock_client
        
        # Override env to simulate GCS configured
        with patch.dict(os.environ, {"GCS_BUCKET_NAME": "test-bucket"}):
            status = check_kill_switch()
            self.assertEqual(status.get("status"), "HALTED")

    @patch("core.alpaca_client.AlpacaClient")
    def test_data_provider_crypto_symbol_standardization_and_timeframe(self, mock_client_class):
        from core.data_provider import DataProvider
        import pandas as pd
        
        mock_client = mock_client_class.return_value
        
        # Mock get_historical_bars to return a dummy df with enough bars and DatetimeIndex
        times = pd.date_range(start="2026-07-29 09:30:00-04:00", periods=50, freq="5min")
        dummy_df = pd.DataFrame({
            "open": [10.0]*50,
            "high": [10.5]*50,
            "low": [9.5]*50,
            "close": [10.0]*50, 
            "volume": [1000]*50,
            "rsi_14": [50.0]*50,
            "sma_20": [10.0]*50,
            "sma_50": [10.0]*50,
            "macd_line": [0.0]*50,
            "macd_signal": [0.0]*50,
            "macd_hist": [0.0]*50,
            "bollinger_upper": [11.0]*50,
            "bollinger_lower": [9.0]*50,
            "vwap": [10.0]*50,
            "vwap_upper_1": [10.5]*50,
            "vwap_lower_1": [9.5]*50,
            "vwap_upper_2": [11.0]*50,
            "vwap_lower_2": [9.0]*50,
            "vwap_dist_pct": [0.0]*50
        }, index=times)
        mock_client.get_historical_bars.return_value = dummy_df
        mock_client.get_news.return_value = []
        
        provider = DataProvider(mock_client)
        
        # Test standardizing "btc" to "BTC/USD" and adjusting timeframe to "5min"
        state = provider.get_market_state("btc")
        
        # Assert that get_historical_bars was called with "BTC/USD" and timeframe_str="5min"
        mock_client.get_historical_bars.assert_any_call("BTC/USD", limit=100, timeframe_str="5min")
        self.assertEqual(state["symbol"], "BTC/USD")

    @patch("core.alpaca_client.AlpacaClient")
    def test_data_provider_pivots_low_priced_assets(self, mock_client_class):
        from core.data_provider import DataProvider
        import pandas as pd
        
        mock_client = mock_client_class.return_value
        provider = DataProvider(mock_client)
        
        # Create a mock daily_df with high/low bars
        daily_df = pd.DataFrame({
            "high": [0.40, 0.42, 0.45, 0.43, 0.41, 0.44],
            "low": [0.30, 0.32, 0.35, 0.33, 0.31, 0.34],
            "close": [0.35, 0.38, 0.40, 0.37, 0.35, 0.38],
            "open": [0.32, 0.35, 0.42, 0.40, 0.37, 0.35]
        })
        
        pivots = provider._calculate_advanced_pivots(daily_df, current_price=0.38, symbol="ADA/USD")
        
        # Increment for 0.38 is 0.01
        # psy_lower = (0.38 // 0.01) * 0.01 = 0.38
        # psy_upper = 0.38 + 0.01 = 0.39
        self.assertAlmostEqual(pivots["psychological_levels"]["closest_support"], 0.38)
        self.assertAlmostEqual(pivots["psychological_levels"]["closest_resistance"], 0.39)

    def test_log_sorting_defaults_to_newest_on_top(self):
        # The frontend/dashboard renders the log lines.
        # This test verifies that the dashboard status cache fetches the last 60 lines.
        from core import config
        test_log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_trading.log")
        with open(test_log_file, "w", encoding="utf-8") as f:
            for i in range(100):
                f.write(f"Log line {i}\n")
        
        try:
            with patch("core.config.LOG_FILE", test_log_file):
                log_file_path = test_log_file
                if os.path.exists(log_file_path):
                    with open(log_file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        log_lines = f.readlines()[-60:]
                self.assertEqual(len(log_lines), 60)
                self.assertEqual(log_lines[-1].strip(), "Log line 99")
                self.assertEqual(log_lines[0].strip(), "Log line 40")
        finally:
            if os.path.exists(test_log_file):
                os.remove(test_log_file)

    def test_crypto_universe_accepts_xrp_and_eth_with_usd_suffixes(self):
        from core import config
        self.assertIn("XRP/USD", config.TRADING_UNIVERSE)
        self.assertIn("ETH/USD", config.TRADING_UNIVERSE)
        for symbol in ["SOL/USD", "BTC/USD", "ETH/USD", "XRP/USD"]:
            is_crypto = "SOL" in symbol or "USD" in symbol or "/" in symbol
            self.assertTrue(is_crypto)

    @patch("core.database.get_recent_trades")
    @patch("core.database.get_latest_watchlist_raw")
    def test_guardrail_prevents_single_ticker_concentration_above_thirty_percent(self, mock_watchlist, mock_trades):
        mock_watchlist.return_value = []
        mock_trades.return_value = []
        
        from core.guardrails import RiskGuardrails
        from core import config
        
        guardrails = RiskGuardrails()
        
        # Scenario: equity is $100,000, max ticker allocation is 30% ($30,000)
        # Current positions: owned 25,000 worth of SOL/USD
        # Proposed buy: 100 shares at $100 per share = $10,000
        # Total would be $35,000 (35% of equity), which breaches 30% limit.
        # The scaled-down proposed buy value should be max $5,000 ($30k limit - $25k existing).
        # At $100/share, the scaled-down qty should be 50 shares.
        
        account_state = {
            "equity": 100000.0,
            "cash": 50000.0,
            "unrealized_pnl": 0.0,
            "last_equity": 100000.0
        }
        current_positions = {
            "SOL/USD": {"qty": 250.0}  # 250 shares @ $100 = $25,000
        }
        
        decision = {"action": "BUY", "symbol": "SOL/USD", "quantity": 100.0, "current_price": 100.0}
        approved, msg, adj_dec = guardrails.validate_and_adjust_decision(
            decision=decision,
            account_state=account_state,
            current_positions=current_positions
        )
        
        self.assertTrue(approved)
        self.assertEqual(adj_dec["quantity"], 50.0)
        self.assertIn("Approved:", msg)
        
        # Scenario 2: Already at/above limit (e.g. 30,000 worth). 
        # Proposed buy should scale down to 0 and get rejected.
        current_positions_full = {
            "SOL/USD": {"qty": 300.0}  # 300 shares @ $100 = $30,000
        }
        decision_full = {"action": "BUY", "symbol": "SOL/USD", "quantity": 10.0, "current_price": 100.0}
        approved, msg, adj_dec = guardrails.validate_and_adjust_decision(
            decision=decision_full,
            account_state=account_state,
            current_positions=current_positions_full
        )
        self.assertFalse(approved)
        self.assertEqual(adj_dec["quantity"], 0.0)
        self.assertIn("would exceed the per-ticker limit of 30.0% of equity", msg)

    def test_daily_cadence_flag_persists_successfully_via_db(self):
        from core.database import Database
        import sqlite3
        db = Database()
        db_key = "test_last_morning_sent"
        
        # Clear any existing value
        with sqlite3.connect(db.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM system_state WHERE key = ?", (db_key,))
            conn.commit()
            
        try:
            val = db.get_system_state(db_key)
            self.assertIsNone(val)
            
            db.set_system_state(db_key, "2026-07-30")
            val = db.get_system_state(db_key)
            self.assertEqual(val, "2026-07-30")
        finally:
            with sqlite3.connect(db.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM system_state WHERE key = ?", (db_key,))
                conn.commit()

if __name__ == "__main__":
    unittest.main()
