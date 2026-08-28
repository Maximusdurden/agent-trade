# filename: tests/test_intraday_breaker.py
"""Tests for the intra-day PnL circuit breaker.

Verifies that a BUY is blocked when the day's realized + unrealized loss exceeds
the intra-day limit, while a healthy day allows BUYs.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

os.environ["DATABASE_FILENAME"] = "test_intraday_breaker.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config
from core.database import init_db, get_db_connection
from core.guardrails import RiskGuardrails


def _clean():
    with get_db_connection() as conn:
        conn.execute("DELETE FROM trades")
        conn.commit()


def _add_today_loss(symbol, buy_price, sell_price, qty=1.0):
    """Insert a buy then a sell closed today (5h ago, still today in Eastern) producing a realized loss.

    Uses a timestamp 5h ago so the anti-whipsaw (4h hold) guardrail does not
    interfere, while still falling on today's Eastern date.
    """
    ts = (datetime.utcnow() - timedelta(hours=5)).isoformat()
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO trades (decision_id, alpaca_order_id, timestamp, symbol, side, qty, filled_avg_price, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, f"{symbol}-b-{ts}", ts, symbol.upper(), "buy", qty, buy_price, "filled"),
        )
        cur.execute(
            "INSERT INTO trades (decision_id, alpaca_order_id, timestamp, symbol, side, qty, filled_avg_price, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (2, f"{symbol}-s-{ts}", ts, symbol.upper(), "sell", qty, sell_price, "filled"),
        )
        conn.commit()
    # Clear the feedback memo so today_realized_pnl sees the new trades.
    import core.feedback as fb
    fb._memo.clear()


class TestIntradayBreaker(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()
        import core.feedback as fb
        fb._memo.clear()
        self.guardrails = RiskGuardrails()

    def tearDown(self):
        _clean()

    def _decision(self, symbol, qty=10.0, price=100.0):
        return {
            "action": "BUY",
            "symbol": symbol,
            "quantity": qty,
            "current_price": price,
            "conviction": 0.7,
            "direction": "bullish",
        }

    def _account(self, equity=100000.0, unrealized=0.0):
        return {"equity": equity, "cash": 60000.0, "unrealized_pnl": unrealized, "last_equity": equity}

    def test_healthy_day_allows_buy(self):
        # No losses today, no unrealized loss -> BUY allowed.
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("SOL/USD"), self._account(), {}
        )
        self.assertTrue(approved, msg)

    def test_realized_loss_blocks_buy(self):
        # Realized loss today of -$5000 on $100k equity = -5% >= 4% limit -> blocked.
        _add_today_loss("SOL/USD", buy_price=100.0, sell_price=50.0, qty=100.0)  # -$5000
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("SOL/USD"), self._account(), {}
        )
        self.assertFalse(approved)
        self.assertEqual(adj["quantity"], 0.0)
        self.assertIn("intra-day", msg.lower())

    def test_unrealized_loss_blocks_buy(self):
        # No realized loss but large unrealized loss -> blocked.
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("SOL/USD"), self._account(unrealized=-5000.0), {}
        )
        self.assertFalse(approved)
        self.assertIn("intra-day", msg.lower())

    def test_small_loss_allows_buy(self):
        # Small realized loss below limit -> allowed.
        _add_today_loss("SOL/USD", buy_price=100.0, sell_price=99.0, qty=10.0)  # -$10
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("SOL/USD"), self._account(), {}
        )
        self.assertTrue(approved, msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)