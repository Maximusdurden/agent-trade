# filename: tests/test_oversized_losing_position.py
"""Tests for the oversized-losing-position force-exit guardrail (2026-09-12).

Verifies that a held position at/above the per-ticker cap AND trading below its
average entry is force-exited (full SELL) regardless of what the brain proposes,
preventing the DOT/USD trap where a position scaled in to ~29% of equity while
price fell below entry and the brain never generated a SELL to de-risk.
"""
import os
import sys
import unittest

os.environ["DATABASE_FILENAME"] = "test_oversized_losing_position.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config
from core.guardrails import RiskGuardrails


def _account(equity=100000.0):
    return {"equity": equity, "cash": 60000.0, "unrealized_pnl": 0.0, "last_equity": equity}


def _decision(symbol, action="HOLD", qty=0.0, price=1.04):
    return {
        "action": action,
        "symbol": symbol,
        "quantity": qty,
        "current_price": price,
        "conviction": 0.5,
        "direction": "neutral",
    }


class TestOversizedLosingPosition(unittest.TestCase):
    def setUp(self):
        self.guardrails = RiskGuardrails()
        # Force market-open so the guardrail reaches the force-exit logic.
        self.guardrails.is_market_open_check = lambda: (True, "open")
        self._orig_frac = getattr(config, "FORCE_EXIT_AT_CAP_FRACTION", 1.0)
        config.FORCE_EXIT_AT_CAP_FRACTION = 1.0

    def tearDown(self):
        config.FORCE_EXIT_AT_CAP_FRACTION = self._orig_frac

    def test_oversized_losing_position_force_exits(self):
        """A position at the cap AND below entry is force-exited even on HOLD."""
        # 30% of $100k = $30k cap. Position: 29,000 shares @ $1.04 = $30,160 (at cap).
        # Avg entry $1.10, current $1.04 (below entry) -> force exit.
        positions = {"DOT/USD": {"qty": 29000.0, "avg_entry_price": 1.10}}
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            _decision("DOT/USD", action="HOLD", qty=0.0, price=1.04),
            _account(), positions
        )
        self.assertTrue(approved)
        self.assertEqual(adj["action"], "SELL")
        self.assertEqual(adj["quantity"], 29000.0)
        self.assertIn("Force-exit", msg)

    def test_oversized_but_profitable_not_force_exited(self):
        """A position at the cap but ABOVE entry (profitable) is NOT force-exited."""
        # Avg entry $1.00, current $1.04 (above entry) -> not a losing position.
        positions = {"DOT/USD": {"qty": 29000.0, "avg_entry_price": 1.00}}
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            _decision("DOT/USD", action="HOLD", qty=0.0, price=1.04),
            _account(), positions
        )
        # HOLD should pass through normally (no force-exit).
        self.assertTrue(approved)
        self.assertNotIn("Force-exit", msg)

    def test_below_cap_not_force_exited(self):
        """A losing position BELOW the cap is NOT force-exited."""
        # 10% of $100k = $10k. Position: 5,000 shares @ $1.04 = $5,200 (below cap).
        positions = {"DOT/USD": {"qty": 5000.0, "avg_entry_price": 1.10}}
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            _decision("DOT/USD", action="HOLD", qty=0.0, price=1.04),
            _account(), positions
        )
        self.assertTrue(approved)
        self.assertNotIn("Force-exit", msg)

    def test_buy_at_cap_rejected_clear_message(self):
        """A BUY on a position already at the cap is rejected with a clear message."""
        positions = {"DOT/USD": {"qty": 29000.0, "avg_entry_price": 1.10}}
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            _decision("DOT/USD", action="BUY", qty=100.0, price=1.04),
            _account(), positions
        )
        # Force-exit fires first (position is at cap AND losing) -> SELL, not BUY.
        self.assertTrue(approved)
        self.assertEqual(adj["action"], "SELL")
        self.assertIn("Force-exit", msg)


if __name__ == "__main__":
    unittest.main()