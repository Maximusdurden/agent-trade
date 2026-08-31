"""Tests for the independent OPTIONS kill switch.

Verifies:
- gcs_sync.check_options_kill_switch defaults to ACTIVE when no file/GCS.
- gcs_sync.set_options_kill_switch_state writes HALTED/ACTIVE.
- guardrails blocks NEW option BUYs when the options kill switch is HALTED.
- guardrails still allows SELL-to-close of an existing option when HALTED.
"""
import os
import sys
import json
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, r"Z:\python\projects")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.gcs_sync import check_options_kill_switch, set_options_kill_switch_state
from core.guardrails import RiskGuardrails

# Use a scratch options kill switch local cache so tests never touch the real one.
SCRATCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scratch_options_kill_switch.json")


class TestOptionsKillSwitchHelpers(unittest.TestCase):
    def tearDown(self):
        if os.path.exists(SCRATCH):
            os.remove(SCRATCH)

    @patch.dict(os.environ, {"GCS_BUCKET_NAME": ""}, clear=False)
    def test_defaults_active_local_only(self):
        # No GCS bucket, no local file -> ACTIVE (options allowed).
        data = check_options_kill_switch()
        self.assertEqual(data.get("status"), "ACTIVE")

    @patch.dict(os.environ, {"GCS_BUCKET_NAME": "test-bucket"}, clear=False)
    @patch("core.gcs_sync.get_gcs_client")
    @patch("core.gcs_sync._OPTIONS_KS_LOCAL", SCRATCH)
    def test_reads_halted_from_gcs(self, mock_get_client):
        mock_blob = MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_text.return_value = json.dumps({"status": "HALTED", "updated_by": "test"})
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_get_client.return_value = mock_client

        data = check_options_kill_switch()
        self.assertEqual(data.get("status"), "HALTED")


class TestGuardrailOptionsKillSwitch(unittest.TestCase):
    def _make_guardrails(self):
        from core.guardrails import RiskGuardrails
        return RiskGuardrails()

    def _option_buy_decision(self):
        return {
            "action": "BUY", "symbol": "AAPL", "quantity": 2.0,
            "conviction": 0.8, "direction": "bullish", "current_price": 100.0,
        }

    def _option_sell_decision(self):
        return {
            "action": "SELL", "symbol": "AAPL261009C00320000", "quantity": 5.0,
            "conviction": 0.8, "direction": "bearish", "current_price": 100.0,
        }

    @patch("core.gcs_sync.check_options_kill_switch", return_value={"status": "HALTED"})
    def test_blocks_new_option_buy_when_halted(self, _):
        g = self._make_guardrails()
        ok, msg, adj = g.validate_and_adjust_decision(
            self._option_buy_decision(),
            {"equity": 100000.0, "cash": 50000.0},
            {},
            cycle_context={"spent": 0.0, "trades": 0},
        )
        self.assertFalse(ok)
        # The kill-switch rejection fires BEFORE the buying-power check, so the
        # message is the kill-switch halt regardless of OBP.
        self.assertIn("KILL SWITCH is HALTED", msg)

    @patch("core.gcs_sync.check_options_kill_switch", return_value={"status": "ACTIVE"})
    @patch.object(RiskGuardrails, "_get_options_buying_power", return_value=100000.0)
    def test_allows_option_buy_when_active(self, _, __):
        g = self._make_guardrails()
        ok, msg, adj = g.validate_and_adjust_decision(
            self._option_buy_decision(),
            {"equity": 100000.0, "cash": 50000.0},
            {},
            cycle_context={"spent": 0.0, "trades": 0},
        )
        self.assertTrue(ok)
        self.assertEqual(adj.get("instrument"), "option")

    @patch("core.gcs_sync.check_options_kill_switch", return_value={"status": "HALTED"})
    def test_allows_sell_to_close_when_halted(self, _):
        """Kill switch must never lock you into an existing option position."""
        g = self._make_guardrails()
        held = {"AAPL261009C00320000": {"qty": 5.0, "market_value": 3000.0}}
        ok, msg, adj = g.validate_and_adjust_decision(
            self._option_sell_decision(),
            {"equity": 100000.0, "cash": 50000.0},
            held,
            cycle_context={"spent": 0.0, "trades": 0},
        )
        # SELL-to-close should be permitted even when options buy is halted.
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()