# filename: tests/test_dust_liquidation.py
"""Tests for the MIN_SELL_VALUE dust-liquidation guardrail.

Verifies that a SELL whose position value (or proposed sell value) is below
MIN_SELL_VALUE is escalated to a full liquidation instead of bleeding out a
tiny fraction every cycle.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config
from core.guardrails import RiskGuardrails


class TestDustLiquidation(unittest.TestCase):
    def setUp(self):
        self.guardrails = RiskGuardrails()

    def _decision(self, qty, price=76.0):
        return {
            "action": "SELL",
            "symbol": "SOL/USD",
            "quantity": qty,
            "current_price": price,
        }

    def _account(self):
        return {"equity": 100000.0, "cash": 50000.0, "unrealized_pnl": 0.0, "last_equity": 100000.0}

    def _positions(self, owned, available=None):
        return {"SOL/USD": {"qty": owned, "qty_available": available if available is not None else owned}}

    def test_dust_position_escalates_to_full_exit(self):
        # Whole position worth ~$0.98 (< MIN_SELL_VALUE) -> full liquidation
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision(qty=0.012895526, price=76.0),
            self._account(),
            self._positions(owned=0.012895526),
        )
        self.assertTrue(approved)
        self.assertEqual(adj["quantity"], 0.012895526)
        self.assertIn("Dust-liquidation", msg)

    def test_proposed_dust_sell_escalates_to_full_exit(self):
        # Position is large but the proposed sell (25% of it) is below MIN_SELL_VALUE
        owned = 0.229
        proposed = owned * 0.25  # 0.05725 shares ~ $4.35 < $50
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision(qty=proposed, price=76.0),
            self._account(),
            self._positions(owned=owned),
        )
        self.assertTrue(approved)
        self.assertEqual(adj["quantity"], owned)  # escalated to full exit
        self.assertIn("Dust-liquidation", msg)

    def test_normal_sell_unchanged(self):
        # A normal sell above MIN_SELL_VALUE is not escalated
        owned = 10.0
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision(qty=2.5, price=76.0),
            self._account(),
            self._positions(owned=owned),
        )
        self.assertTrue(approved)
        self.assertEqual(adj["quantity"], 2.5)
        self.assertNotIn("full liquidation", msg)

    def test_min_sell_value_config_present(self):
        self.assertGreater(config.MIN_SELL_VALUE, 0)


if __name__ == "__main__":
    unittest.main()