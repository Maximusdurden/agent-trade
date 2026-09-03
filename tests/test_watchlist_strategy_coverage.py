import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config, database
from core.strategist import build_strategy_universe
from runner import (
    build_appraisal_universe,
    check_execution_window,
    ensure_active_strategy,
)
from core.strategy_rules import is_crypto_symbol, normalize_symbol, validate_strategy_rule


class TestWatchlistStrategyCoverage(unittest.TestCase):
    def test_missing_strategy_has_no_generic_fallback(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        connection = MagicMock()
        connection.__enter__.return_value.cursor.return_value = cursor

        with patch("core.database.get_db_connection", return_value=connection):
            result = database.get_active_strategy("nee")

        self.assertEqual(result, "No active strategy rules defined for NEE.")
        cursor.execute.assert_called_once()

    def test_off_hours_universe_excludes_all_equities(self):
        screened = ["SPY", "SOL/USD", "NEE"]
        positions = {
            "NEE": {"qty": 2},
            "BTCUSD": {"qty": 0.1},
            "SOL/USD": {"qty": 1},
        }

        universe = build_appraisal_universe(screened, positions, actual_market_open=False)

        self.assertEqual(universe, ["SOL/USD", "BTCUSD"])
        self.assertNotIn("SPY", universe)
        self.assertNotIn("NEE", universe)

    def test_market_hours_universe_includes_watchlist_and_holdings_once(self):
        universe = build_appraisal_universe(
            ["AAPL", "SOL/USD"],
            {"NEE": {"qty": 2}, "AAPL": {"qty": 1}},
            actual_market_open=True,
        )

        self.assertEqual(universe, ["AAPL", "SOL/USD", "NEE"])

    def test_crypto_detection_does_not_misclassify_equity_with_usd_letters(self):
        self.assertTrue(is_crypto_symbol("SOL/USD"))
        self.assertTrue(is_crypto_symbol("BTCUSD"))
        self.assertFalse(is_crypto_symbol("USD"))
        self.assertFalse(is_crypto_symbol("USDP"))
        self.assertFalse(is_crypto_symbol("NEE"))

    def test_crypto_symbol_normalization_accepts_compact_pairs(self):
        self.assertEqual(normalize_symbol("btcusd"), "BTC/USD")
        self.assertEqual(normalize_symbol("SOL-USD"), "SOL/USD")

    def test_crypto_rule_rejects_equity_only_mandate(self):
        valid, reason = validate_strategy_rule(
            "BTC/USD",
            "Only trade SPY and QQQ when their RSI is below 40; otherwise hold.",
        )
        self.assertFalse(valid)
        self.assertEqual(reason, "crypto_rule_scoped_to_equity_indices")

    def test_crypto_rule_rejects_observed_spy_qqq_rule_without_only_keyword(self):
        valid, reason = validate_strategy_rule(
            "ETH/USD",
            "Focus on conservative growth. Trade SPY and QQQ when RSI is below 40. Avoid over-trading.",
        )
        self.assertFalse(valid)
        self.assertEqual(reason, "crypto_rule_scoped_to_equity_indices")

    def test_crypto_rule_accepts_ticker_specific_threshold(self):
        valid, reason = validate_strategy_rule(
            "BTC/USD",
            "If BTC falls 3% below VWAP, then hold; buy BTC only near confirmed support.",
        )
        self.assertTrue(valid)
        self.assertEqual(reason, "valid")

    @patch("runner.datetime")
    def test_equity_window_rejects_weekday_premarket(self, datetime_mock):
        datetime_mock.now.return_value = __import__("datetime").datetime(2026, 8, 3, 9, 29)
        client = MagicMock(is_mock=True)

        allowed, reason = check_execution_window(client)

        self.assertFalse(allowed)
        self.assertIn("09:30 - 16:30", reason)

    @patch("runner.datetime")
    def test_equity_window_accepts_standard_market_open(self, datetime_mock):
        datetime_mock.now.return_value = __import__("datetime").datetime(2026, 8, 3, 9, 30)
        client = MagicMock(is_mock=True)

        allowed, _ = check_execution_window(client)

        self.assertTrue(allowed)

    @patch("core.strategist.MetaStrategist")
    @patch("runner.database.get_system_state", return_value=None)
    @patch("runner.database.set_system_state")
    @patch("runner.database.get_active_strategy")
    def test_missing_strategy_is_regenerated_and_rechecked(self, get_rule, set_state, get_state, strategist_class):
        get_rule.side_effect = [
            "No active strategy rules defined for SOL/USD.",
            "If SOL/USD falls below support, then reduce exposure.",
        ]

        self.assertTrue(ensure_active_strategy("SOL/USD", MagicMock()))
        strategist_class.return_value.run_single_ticker_refinement.assert_called_once()
        self.assertEqual(get_rule.call_count, 2)

    @patch("core.strategist.MetaStrategist")
    @patch("runner.database.get_system_state", return_value=None)
    @patch("runner.database.set_system_state")
    @patch("runner.database.get_active_strategy")
    def test_asset_is_skipped_when_regeneration_does_not_persist_rule(self, get_rule, set_state, get_state, strategist_class):
        get_rule.return_value = "No active strategy rules defined for SOL/USD."

        self.assertFalse(ensure_active_strategy("SOL/USD", MagicMock()))
        strategist_class.return_value.run_single_ticker_refinement.assert_called_once()
        self.assertEqual(get_rule.call_count, 2)

    def test_daily_strategy_universe_covers_config_holdings_and_pool(self):
        universe = build_strategy_universe(
            {"NEE": {"qty": 2}, "SOL/USD": {"qty": 1}},
            ["NEE", "VRTX"],
        )

        self.assertTrue(set(config.TRADING_UNIVERSE).issubset(universe))
        self.assertIn("NEE", universe)
        self.assertIn("VRTX", universe)
        self.assertEqual(len(universe), len(set(universe)))

    def test_daily_strategy_universe_excludes_occ_option_contracts(self):
        # A held OCC option contract must NOT be treated as a stock for
        # strategy generation (it would hit the stock bars endpoint and fail).
        universe = build_strategy_universe(
            {"NVDA": {"qty": 30}, "NVDA261016C00230000": {"qty": 4}},
            ["NVDA"],
        )

        self.assertIn("NVDA", universe)
        self.assertNotIn("NVDA261016C00230000", universe)


if __name__ == "__main__":
    unittest.main()