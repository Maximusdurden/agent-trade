"""Tests for the strategist's instrument_hint authorization feature.

Verifies:
1. `log_strategy_history` persists `instrument_hint`.
2. `get_active_strategy_hint` retrieves the latest hint (normalized lowercase).
3. The brain's prompt includes the STRATEGIST INSTRUMENT AUTHORIZATION line.
4. `build_appraisal_universe` includes symbols with an "option" hint.
5. The total options exposure cap blocks a new option BUY when exposure is at/above cap.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, r"Z:\python\projects")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import database
from core.guardrails import RiskGuardrails


def _seed_watchlist(symbols):
    import json
    with database.get_db_connection() as conn:
        conn.execute("DELETE FROM watchlist_history")
        conn.execute("INSERT INTO watchlist_history (timestamp, watchlist) VALUES (?, ?)",
                     ("2026-08-15 12:00:00", json.dumps(symbols)))
        conn.commit()


class TestStrategistInstrumentHint(unittest.TestCase):
    def setUp(self):
        with database.get_db_connection() as conn:
            conn.execute("DELETE FROM strategy_history")
            conn.commit()

    def test_log_and_retrieve_hint(self):
        database.log_strategy_history(
            ticker="NVDA", yesterdays_rules="old", todays_rules="new rule",
            meta_reasoning="bearish", strategy_version="v1", instrument_hint="option",
        )
        hint = database.get_active_strategy_hint("NVDA")
        self.assertEqual(hint, "option")

    def test_hint_normalized_lowercase(self):
        database.log_strategy_history(
            ticker="AAPL", yesterdays_rules="old", todays_rules="new",
            meta_reasoning="bullish", strategy_version="v1", instrument_hint="OPTION",
        )
        hint = database.get_active_strategy_hint("AAPL")
        self.assertEqual(hint, "option")

    def test_no_hint_returns_none(self):
        database.log_strategy_history(
            ticker="MSFT", yesterdays_rules="old", todays_rules="new",
            meta_reasoning="neutral", strategy_version="v1",
        )
        hint = database.get_active_strategy_hint("MSFT")
        self.assertIsNone(hint)

    def test_brain_prompt_includes_authorization(self):
        from core.trading_brain import TradingBrain
        brain = TradingBrain()
        brain.is_mock = True
        # Seed a hint for NVDA
        database.log_strategy_history(
            ticker="NVDA", yesterdays_rules="old", todays_rules="new",
            meta_reasoning="bearish", strategy_version="v1", instrument_hint="option",
        )
        market_data = [{
            "symbol": "NVDA", "current_price": 100.0, "daily_return_pct": -0.03,
            "indicators": {"rsi_14": 30, "sma_20": 105, "sma_50": 110,
                           "macd_line": -1, "macd_signal": -0.5, "macd_hist": -0.5,
                           "bollinger_upper": 120, "bollinger_lower": 90,
                           "vwap": 101, "vwap_upper_1": 102, "vwap_lower_1": 100,
                           "vwap_upper_2": 103, "vwap_lower_2": 99, "vwap_dist_pct": -0.01},
            "advanced_pivots": {}, "news": [],
        }]
        prompt = brain._build_prompt(market_data, {"equity": 100000.0}, {}, [])
        self.assertIn("STRATEGIST INSTRUMENT AUTHORIZATION", prompt)
        self.assertIn('"option"', prompt)

    def test_appraisal_universe_includes_option_hint(self):
        from runner import build_appraisal_universe
        database.log_strategy_history(
            ticker="GOOGL", yesterdays_rules="old", todays_rules="new",
            meta_reasoning="bearish", strategy_version="v1", instrument_hint="option",
        )
        universe = build_appraisal_universe(["NVDA"], {}, True)
        self.assertIn("GOOGL", universe)

    def test_total_options_exposure_cap_blocks_buy(self):
        _seed_watchlist(["NVDA"])
        g = RiskGuardrails()
        decision = {"action": "BUY", "symbol": "NVDA", "quantity": 1.0,
                    "conviction": 0.9, "direction": "bearish", "instrument": "option",
                    "current_price": 100.0}
        # Simulate an existing option position already at the cap (15% of 100k = 15k).
        positions = {"NVDA261016C00230000": {"qty": 1, "market_value": 15000.0}}
        with patch("core.gcs_sync.check_options_kill_switch", return_value={"status": "ACTIVE"}), \
             patch.object(RiskGuardrails, "_get_options_buying_power", return_value=100000.0), \
             patch.object(RiskGuardrails, "is_market_open_check", return_value=(True, "open")):
            ok, msg, adj = g.validate_and_adjust_decision(
                decision, {"equity": 100000.0, "cash": 50000.0}, positions,
                cycle_context={"spent": 0.0, "trades": 0},
            )
        self.assertFalse(ok)
        self.assertIn("Total options exposure", msg)


if __name__ == "__main__":
    unittest.main()
