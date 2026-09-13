import os
import sys
import unittest
from unittest.mock import patch

os.environ["DATABASE_FILENAME"] = "test_strategist_ab.db"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import config
from core.strategist import MetaStrategist


class TestStrategistAB(unittest.TestCase):
    def test_pick_ab_model_disabled_when_single_or_empty(self):
        with patch.object(config, "STRATEGIST_AB_MODELS", ""):
            self.assertIsNone(MetaStrategist._pick_ab_model())
        with patch.object(config, "STRATEGIST_AB_MODELS", "deepseek/deepseek-r1"):
            self.assertIsNone(MetaStrategist._pick_ab_model())

    def test_pick_ab_model_alternates_between_two(self):
        with patch.object(config, "STRATEGIST_AB_MODELS", "deepseek/deepseek-r1,anthropic/claude-sonnet-5"):
            m = MetaStrategist._pick_ab_model()
            self.assertIn(m, ("deepseek/deepseek-r1", "anthropic/claude-sonnet-5"))

    def test_pick_ab_model_strips_whitespace_and_empty_entries(self):
        with patch.object(config, "STRATEGIST_AB_MODELS", "  a/b ,,  c/d  "):
            m = MetaStrategist._pick_ab_model()
            self.assertIn(m, ("a/b", "c/d"))

    def test_strategy_version_embeds_model_tag(self):
        from core.feedback import next_strategy_version
        model = "anthropic/claude-sonnet-5"
        model_tag = model.replace("/", "-").replace("_", "-")
        ver = f"{next_strategy_version()}|model={model_tag}"
        self.assertTrue(ver.startswith("v"))
        self.assertIn("|model=anthropic-claude-sonnet-5", ver)

    def test_report_norm_model_merges_all_tag_formats(self):
        """The report must merge raw, normalized, and options-track tags into one bucket."""
        from tools.strategist_ab_report import norm_model, MODEL_RE

        def norm_version(ver):
            m = MODEL_RE.search(ver)
            return norm_model(m.group(1) if m else "unknown")

        # Same model, different historical formats -> all normalize to one key.
        self.assertEqual(
            norm_version("v1|model=deepseek/deepseek-r1"),
            "deepseek-deepseek-r1",
        )
        self.assertEqual(
            norm_version("v2|model=deepseek-deepseek-r1|track=options"),
            "deepseek-deepseek-r1",
        )
        self.assertEqual(
            norm_version("v3|model=anthropic/claude-sonnet-5"),
            "anthropic-claude-sonnet-5",
        )
        self.assertEqual(
            norm_version("v4|model=anthropic-claude-sonnet-5|track=options"),
            "anthropic-claude-sonnet-5",
        )
        # Unknown / empty -> unknown.
        self.assertEqual(norm_model(""), "unknown")
        self.assertEqual(norm_model("unknown"), "unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)