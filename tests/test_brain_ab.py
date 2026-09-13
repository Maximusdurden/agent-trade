import os
import sys
import unittest
from unittest.mock import patch

os.environ["DATABASE_FILENAME"] = "test_brain_ab.db"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import config
from core.trading_brain import TradingBrain


class TestBrainAB(unittest.TestCase):
    def test_pick_ab_model_disabled_when_single_or_empty(self):
        with patch.object(config, "BRAIN_AB_MODELS", ""):
            self.assertIsNone(TradingBrain._pick_ab_model())
        with patch.object(config, "BRAIN_AB_MODELS", "google/gemini-2.5-flash"):
            self.assertIsNone(TradingBrain._pick_ab_model())

    def test_pick_ab_model_alternates_between_two(self):
        with patch.object(config, "BRAIN_AB_MODELS", "google/gemini-2.5-flash,google/gemini-2.5-pro"):
            m = TradingBrain._pick_ab_model()
            self.assertIn(m, ("google/gemini-2.5-flash", "google/gemini-2.5-pro"))

    def test_pick_ab_model_strips_whitespace_and_empty_entries(self):
        with patch.object(config, "BRAIN_AB_MODELS", "  a/b ,,  c/d  "):
            m = TradingBrain._pick_ab_model()
            self.assertIn(m, ("a/b", "c/d"))

    def test_stamp_model_uses_ab_model_when_set(self):
        brain = object.__new__(TradingBrain)
        brain.ab_model = "google/gemini-2.5-pro"
        decisions = [{"symbol": "AAPL"}, {"symbol": "MSFT"}]
        out = TradingBrain._stamp_model(brain, decisions)
        self.assertTrue(all(d["model"] == "google/gemini-2.5-pro" for d in out))

    def test_stamp_model_falls_back_to_tier_default(self):
        brain = object.__new__(TradingBrain)
        brain.ab_model = None
        with patch.object(config, "BRAIN_MODEL_TIER", "daily_driver"):
            out = TradingBrain._stamp_model(brain, [{"symbol": "AAPL"}])
        self.assertEqual(out[0]["model"], "daily_driver")


if __name__ == "__main__":
    unittest.main(verbosity=2)