"""Tests that instrument routing RESPECTS the model's explicit intent.

Regression for the NVDA incident: the LLM said "buy shares" (thought_process
stated shares, instrument preference shares) but emitted conviction 0.7 which
the old conviction-threshold rule converted into an option contract. Now the
guardrail uses the model's explicit "instrument" as the primary signal and
conviction as a GATE (options require instrument=="option" AND conviction >=
threshold).
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, r"Z:\python\projects")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.guardrails import RiskGuardrails


def _seed_watchlist(symbols):
    """Seed the current watchlist so strict-universe guardrail passes."""
    import json
    from core import database
    with database.get_db_connection() as conn:
        conn.execute("DELETE FROM watchlist_history")
        conn.execute("INSERT INTO watchlist_history (timestamp, watchlist) VALUES (?, ?)",
                     ("2026-08-15 12:00:00", json.dumps(symbols)))
        conn.commit()


def _run(decision, positions=None):
    g = RiskGuardrails()
    # These tests exercise option-vs-stock routing, not universe gating; endorse NVDA.
    _seed_watchlist(["NVDA"])
    with patch("core.gcs_sync.check_options_kill_switch", return_value={"status": "ACTIVE"}), \
         patch.object(RiskGuardrails, "_get_options_buying_power", return_value=100000.0), \
         patch.object(RiskGuardrails, "is_market_open_check", return_value=(True, "open")):
        return g.validate_and_adjust_decision(
            decision, {"equity": 100000.0, "cash": 50000.0}, positions or {},
            cycle_context={"spent": 0.0, "trades": 0},
        )


class TestInstrumentRouting(unittest.TestCase):
    def _buy(self, instrument, conviction, direction="bullish"):
        return {"action": "BUY", "symbol": "NVDA", "quantity": 10.0,
                "conviction": conviction, "direction": direction,
                "instrument": instrument, "current_price": 100.0}

    def test_stock_intent_never_routes_to_option(self):
        """The exact NVDA case: instrument='stock', conviction 0.7 -> stock."""
        ok, msg, adj = _run(self._buy("stock", 0.7))
        self.assertTrue(ok)
        self.assertEqual(adj.get("instrument"), "stock")

    def test_option_intent_with_high_conviction_routes_to_option(self):
        ok, msg, adj = _run(self._buy("option", 0.8))
        self.assertTrue(ok)
        self.assertEqual(adj.get("instrument"), "option")

    def test_option_intent_but_low_conviction_routed_to_stock(self):
        """Conviction is a GATE: below threshold, even option intent is stock."""
        ok, msg, adj = _run(self._buy("option", 0.6))
        self.assertTrue(ok)
        self.assertEqual(adj.get("instrument"), "stock")

    def test_no_instrument_field_defaults_to_stock(self):
        """Legacy decisions without an instrument field must not route to options."""
        ok, msg, adj = _run(self._buy(None, 0.9))
        self.assertTrue(ok)
        self.assertEqual(adj.get("instrument"), "stock")


if __name__ == "__main__":
    unittest.main()