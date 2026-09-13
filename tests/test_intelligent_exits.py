# filename: tests/test_intelligent_exits.py
"""Tests for the Phase 4 intelligent-exit guardrails.

- MAX_HOLD_HOURS: force-exit a stale position held too long (INTC 464h failure).
- TRAIL_STOP_GIVEBACK_PCT: force-exit a winner that gave back its peak gain
  (NVDA 77.8% win but -$511 failure).
- RSI_EXIT_OVERBOUGHT: force-exit a profitable overbought position.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ["DATABASE_FILENAME"] = "test_intelligent_exits.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config
from core.guardrails import RiskGuardrails


def _account(equity=100000.0):
    return {"equity": equity, "cash": 60000.0, "unrealized_pnl": 0.0, "last_equity": equity}


def _decision(symbol, action="HOLD", qty=0.0, price=1.04, indicators=None):
    return {
        "action": action,
        "symbol": symbol,
        "quantity": qty,
        "current_price": price,
        "conviction": 0.5,
        "direction": "neutral",
        "indicators": indicators or {},
    }


def _positions(symbol, qty, avg_entry):
    return {symbol: {"qty": qty, "avg_entry_price": avg_entry}}


class TestIntelligentExits(unittest.TestCase):
    def setUp(self):
        self.g = RiskGuardrails()
        self.g.is_market_open_check = lambda: (True, "open")
        self._orig = {
            "max_hold": getattr(config, "MAX_HOLD_HOURS", 0),
            "giveback": getattr(config, "TRAIL_STOP_GIVEBACK_PCT", 0),
            "min_gain": getattr(config, "TRAIL_STOP_MIN_GAIN_PCT", 0),
            "rsi": getattr(config, "RSI_EXIT_OVERBOUGHT", 0),
        }

    def tearDown(self):
        config.MAX_HOLD_HOURS = self._orig["max_hold"]
        config.TRAIL_STOP_GIVEBACK_PCT = self._orig["giveback"]
        config.TRAIL_STOP_MIN_GAIN_PCT = self._orig["min_gain"]
        config.RSI_EXIT_OVERBOUGHT = self._orig["rsi"]

    def test_max_hold_exits_stale_position(self):
        """A position held past MAX_HOLD_HOURS is force-exited even on HOLD."""
        config.MAX_HOLD_HOURS = 168  # 7 days
        old_ts = (datetime.utcnow() - timedelta(hours=200)).isoformat()
        with patch("core.database.get_recent_trades_by_symbol",
                   return_value=[{"side": "buy", "status": "filled",
                                  "timestamp": old_ts, "filled_avg_price": 1.10}]):
            approved, msg, adj = self.g.validate_and_adjust_decision(
                _decision("INTC", action="HOLD", qty=0.0, price=1.04),
                _account(), _positions("INTC", 100.0, 1.10)
            )
        self.assertTrue(approved)
        self.assertEqual(adj["action"], "SELL")
        self.assertEqual(adj["quantity"], 100.0)
        self.assertIn("MAX_HOLD_HOURS", msg)

    def test_max_hold_not_exited_when_fresh(self):
        config.MAX_HOLD_HOURS = 168
        fresh_ts = (datetime.utcnow() - timedelta(hours=10)).isoformat()
        with patch("core.database.get_recent_trades_by_symbol",
                   return_value=[{"side": "buy", "status": "filled",
                                  "timestamp": fresh_ts, "filled_avg_price": 1.10}]):
            approved, msg, adj = self.g.validate_and_adjust_decision(
                _decision("AAPL", action="HOLD", qty=0.0, price=1.04),
                _account(), _positions("AAPL", 100.0, 1.10)
            )
        self.assertTrue(approved)
        self.assertEqual(adj["action"], "HOLD")  # not force-exited

    def test_trailing_stop_exits_giveback(self):
        """A winner that gave back >50% of its peak gain is force-exited."""
        config.TRAIL_STOP_GIVEBACK_PCT = 0.50
        config.TRAIL_STOP_MIN_GAIN_PCT = 0.03
        # Avg entry 1.00, peak 1.20 (from trade history), now 1.05.
        # gain_pct = 5% >= 3%; giveback = (1.20-1.05)/(1.20-1.00) = 75% >= 50%.
        with patch("core.database.get_recent_trades_by_symbol",
                   return_value=[{"side": "buy", "status": "filled",
                                  "timestamp": datetime.utcnow().isoformat(),
                                  "filled_avg_price": 1.20}]):
            approved, msg, adj = self.g.validate_and_adjust_decision(
                _decision("NVDA", action="HOLD", qty=0.0, price=1.05),
                _account(), _positions("NVDA", 100.0, 1.00)
            )
        self.assertTrue(approved)
        self.assertEqual(adj["action"], "SELL")
        self.assertIn("gave back", msg)

    def test_trailing_stop_not_exited_when_holding_gain(self):
        config.TRAIL_STOP_GIVEBACK_PCT = 0.50
        config.TRAIL_STOP_MIN_GAIN_PCT = 0.03
        # Peak 1.20, now 1.18 -> giveback (1.20-1.18)/(1.20-1.00) = 10% < 50%.
        with patch("core.database.get_recent_trades_by_symbol",
                   return_value=[{"side": "buy", "status": "filled",
                                  "timestamp": datetime.utcnow().isoformat(),
                                  "filled_avg_price": 1.20}]):
            approved, msg, adj = self.g.validate_and_adjust_decision(
                _decision("NVDA", action="HOLD", qty=0.0, price=1.18),
                _account(), _positions("NVDA", 100.0, 1.00)
            )
        self.assertTrue(approved)
        self.assertEqual(adj["action"], "HOLD")

    def test_rsi_overbought_exits_profitable(self):
        config.RSI_EXIT_OVERBOUGHT = 70
        approved, msg, adj = self.g.validate_and_adjust_decision(
            _decision("XOM", action="HOLD", qty=0.0, price=1.10,
                      indicators={"rsi_14": 75}),
            _account(), _positions("XOM", 100.0, 1.00)
        )
        self.assertTrue(approved)
        self.assertEqual(adj["action"], "SELL")
        self.assertIn("RSI", msg)

    def test_rsi_overbought_not_exited_when_losing(self):
        config.RSI_EXIT_OVERBOUGHT = 70
        # Below entry -> not profitable, no RSI exit.
        approved, msg, adj = self.g.validate_and_adjust_decision(
            _decision("XOM", action="HOLD", qty=0.0, price=0.95,
                      indicators={"rsi_14": 75}),
            _account(), _positions("XOM", 100.0, 1.00)
        )
        self.assertTrue(approved)
        self.assertEqual(adj["action"], "HOLD")

    def test_all_disabled_no_force_exit(self):
        config.MAX_HOLD_HOURS = 0
        config.TRAIL_STOP_GIVEBACK_PCT = 0
        config.RSI_EXIT_OVERBOUGHT = 0
        approved, msg, adj = self.g.validate_and_adjust_decision(
            _decision("AAPL", action="HOLD", qty=0.0, price=1.04),
            _account(), _positions("AAPL", 100.0, 1.10)
        )
        self.assertTrue(approved)
        self.assertEqual(adj["action"], "HOLD")


if __name__ == "__main__":
    unittest.main(verbosity=2)