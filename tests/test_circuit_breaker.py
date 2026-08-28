# filename: tests/test_circuit_breaker.py
"""Tests for the per-ticker loss/whipsaw circuit breaker guardrail.

Verifies that a BUY to a symbol with N consecutive losing round-trips (or a
high whipsaw ratio) is blocked, while a healthy symbol is approved.
"""
import os
import sys
import unittest

# Isolated test DB before importing core modules.
os.environ["DATABASE_FILENAME"] = "test_circuit_breaker.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config
from core.database import init_db, get_db_connection
from core.guardrails import RiskGuardrails


def _clean_trades():
    with get_db_connection() as conn:
        conn.execute("DELETE FROM trades")
        conn.commit()


_order_counter = [0]


def _add_round_trip(symbol, buy_price, sell_price, qty=1.0):
    """Insert a buy then a sell producing one closed round-trip, 5h in the past.

    Timestamps are set 5 hours ago so the anti-whipsaw (4h hold) guardrail does
    not interfere with the circuit-breaker tests. Order IDs are unique per call.
    """
    from datetime import datetime, timedelta
    old = (datetime.utcnow() - timedelta(hours=5)).isoformat()
    _order_counter[0] += 1
    n = _order_counter[0]
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO trades (decision_id, alpaca_order_id, timestamp, symbol, side, qty, filled_avg_price, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, f"{symbol}-b-{n}", old, symbol.upper(), "buy", qty, buy_price, "filled"),
        )
        cur.execute(
            "INSERT INTO trades (decision_id, alpaca_order_id, timestamp, symbol, side, qty, filled_avg_price, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (2, f"{symbol}-s-{n}", old, symbol.upper(), "sell", qty, sell_price, "filled"),
        )
        conn.commit()


class TestCircuitBreaker(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean_trades()
        # Clear the feedback memo cache so each test sees fresh round-trips.
        import core.feedback as fb
        fb._memo.clear()
        self.guardrails = RiskGuardrails()

    def tearDown(self):
        _clean_trades()

    def _decision(self, symbol, qty=10.0, price=100.0):
        return {
            "action": "BUY",
            "symbol": symbol,
            "quantity": qty,
            "current_price": price,
            "conviction": 0.7,
            "direction": "bullish",
        }

    def _account(self, equity=100000.0):
        return {"equity": equity, "cash": 60000.0, "unrealized_pnl": 0.0, "last_equity": equity}

    def test_consecutive_losses_block_buy(self):
        # 3 consecutive losing round-trips (buy high, sell low) on SOL/USD.
        for _ in range(3):
            _add_round_trip("SOL/USD", buy_price=100.0, sell_price=90.0)
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("SOL/USD"), self._account(), {}
        )
        self.assertFalse(approved)
        self.assertEqual(adj["quantity"], 0.0)
        self.assertIn("circuit breaker", msg.lower())

    def test_fewer_than_threshold_losses_allowed(self):
        # Only 2 losses (< MAX_CONSECUTIVE_LOSSES=3) -> allowed.
        for _ in range(2):
            _add_round_trip("SOL/USD", buy_price=100.0, sell_price=90.0)
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("SOL/USD"), self._account(), {}
        )
        self.assertTrue(approved, msg)

    def test_healthy_symbol_approved(self):
        # Winning round-trips -> no breaker.
        _add_round_trip("BTC/USD", buy_price=90.0, sell_price=110.0)
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("BTC/USD"), self._account(), {}
        )
        self.assertTrue(approved, msg)

    def test_whipsaw_trap_blocks_buy(self):
        # Mix of wins and losses but all sub-4h (whipsaw) -> blocked by whipsaw
        # rule, NOT by consecutive-losses (which needs 3 straight losses).
        # Alternate win/loss so no 3-loss streak exists.
        for i in range(6):
            if i % 2 == 0:
                _add_round_trip("SOL/USD", buy_price=90.0, sell_price=100.0)  # win
            else:
                _add_round_trip("SOL/USD", buy_price=100.0, sell_price=99.0)  # loss
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("SOL/USD"), self._account(), {}
        )
        self.assertFalse(approved)
        self.assertIn("whipsaw", msg.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)