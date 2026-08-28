# filename: tests/test_vol_sizing.py
"""Tests for volatility-based position sizing.

Verifies that a BUY to a high-volatility asset (ATR% above baseline) is scaled
down so the same dollar risk is taken regardless of asset, while a low-vol
asset is unaffected.
"""
import os
import sys
import unittest

os.environ["DATABASE_FILENAME"] = "test_vol_sizing.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config
from core.database import init_db, get_db_connection
from core.guardrails import RiskGuardrails


def _clean():
    with get_db_connection() as conn:
        conn.execute("DELETE FROM trades")
        conn.commit()


class TestVolSizing(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()
        import core.feedback as fb
        fb._memo.clear()
        self.guardrails = RiskGuardrails()

    def tearDown(self):
        _clean()

    def _decision(self, symbol, qty, price, atr_pct):
        return {
            "action": "BUY",
            "symbol": symbol,
            "quantity": qty,
            "current_price": price,
            "atr_pct": atr_pct,
            "conviction": 0.8,
            "direction": "bullish",
        }

    def _account(self, equity=100000.0):
        return {"equity": equity, "cash": 80000.0, "unrealized_pnl": 0.0, "last_equity": equity}

    def test_low_vol_unaffected(self):
        # ATR% below baseline (2.0) -> no scaling. SOL/USD is crypto (24/7, in universe).
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("SOL/USD", qty=100, price=100, atr_pct=1.5),
            self._account(), {},
        )
        self.assertTrue(approved, msg)
        self.assertEqual(adj["quantity"], 100)

    def test_high_vol_scaled_down(self):
        # ATR% = 5.0 (> baseline 2.0) -> scale = 2/5 = 0.4. Max trade value = 10% of
        # $100k = $10k. Proposed 100 * $100 = $10k. Capped to $10k * 0.4 = $4k -> qty 40.
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("SOL/USD", qty=100, price=100, atr_pct=5.0),
            self._account(), {},
        )
        self.assertTrue(approved, msg)
        self.assertAlmostEqual(adj["quantity"], 40.0, places=2)

    def test_high_vol_floor_applied(self):
        # Extreme ATR% -> scale floor at VOL_SIZING_MIN_ALLOCATION_PCT (2% of equity).
        # Max trade = $10k; floor = 2% * $100k = $2k. Proposed $10k -> capped to $2k -> qty 20.
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("SOL/USD", qty=100, price=100, atr_pct=50.0),
            self._account(), {},
        )
        self.assertTrue(approved, msg)
        self.assertAlmostEqual(adj["quantity"], 20.0, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)