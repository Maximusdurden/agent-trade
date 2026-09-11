# filename: tests/test_min_notional_and_transient_bars.py
"""Regression tests for the 2026-09-11 TMCL ticket clusters.

Cluster A (TMCL-922..928): a single Alpaca ``backend request timeout`` fanned
out into one ERROR per symbol in the batch-fallback loop, each filing a Jira
ticket. Fix: transient failures are retried and, if they persist, logged as ONE
aggregate WARNING (never ERROR).

Cluster B (TMCL-920/921): a crypto BUY below Alpaca's per-pair minimum was
submitted, rejected with "cost basis must be >= minimal amount of order 10",
then logged as ERROR (client) + CRITICAL (runner) -> two tickets. Fix: a
minimum-notional floor in the guardrails + a typed ``MinimumNotionalError``
pre-check in the client, logged at WARNING.
"""
import os
import sys
import unittest

# Use a SEPARATE test database so tests never pollute the live trading DB and
# the Jira test-env guard (DATABASE_FILENAME starts with "test_") is triggered.
os.environ["DATABASE_FILENAME"] = "test_min_notional.db"

sys.path.insert(0, r"Z:\python\projects")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import core.config as config
from core.alpaca_client import _is_transient_data_error, MinimumNotionalError
from core.database import init_db, get_db_connection
from core.guardrails import RiskGuardrails


def _set_watchlist(syms):
    """Endorse symbols via the latest screener watchlist so the strict-universe
    guardrail lets a new BUY through (mirrors tests/test_anti_scale_in.py)."""
    import json
    with get_db_connection() as conn:
        conn.execute("DELETE FROM watchlist_history")
        conn.execute("INSERT INTO watchlist_history (timestamp, watchlist) VALUES (?, ?)",
                     ("2026-09-11 12:00:00", json.dumps(syms)))
        conn.commit()


class TestTransientErrorDetection(unittest.TestCase):
    def test_timeout_is_transient(self):
        self.assertTrue(_is_transient_data_error(Exception('{"message":"backend request timeout"}')))
        self.assertTrue(_is_transient_data_error(Exception("Request timed out")))
        self.assertTrue(_is_transient_data_error(Exception("503 Service Unavailable")))
        self.assertTrue(_is_transient_data_error(Exception("connection reset by peer")))

    def test_permanent_error_is_not_transient(self):
        self.assertFalse(_is_transient_data_error(Exception("invalid symbol: BNB/USD")))
        self.assertFalse(_is_transient_data_error(Exception("insufficient buying power")))


class TestCryptoMinNotionalGuardrail(unittest.TestCase):
    def setUp(self):
        init_db()
        # Clear the feedback FIFO memo cache so round-trips computed by an
        # earlier module don't trip a per-symbol circuit breaker here.
        import core.feedback as fb
        fb._memo.clear()
        # Endorse the test symbols so the strict-universe guardrail doesn't fire.
        _set_watchlist(["DOT/USD", "AAPL"])
        self.g = RiskGuardrails()
        self.account = {"equity": 100000.0, "cash": 50000.0, "unrealized_pnl": 0.0,
                        "last_equity": 100000.0}

    def _buy(self, symbol, qty, price):
        decision = {
            "action": "BUY", "symbol": symbol, "quantity": qty,
            "current_price": price, "conviction": 0.9,
            "direction": "bullish", "instrument": "stock",
            "indicators": {"rsi_14": 50.0},
        }
        return self.g.validate_and_adjust_decision(decision, self.account, {}, {})

    def test_crypto_buy_below_min_notional_rejected(self):
        # DOT/USD at $4.00, qty 1.0 -> $4.00 notional < $10 minimum.
        approved, reason, adjusted = self._buy("DOT/USD", 1.0, 4.00)
        self.assertFalse(approved)
        self.assertIn("minimum order size", reason)
        self.assertEqual(adjusted["quantity"], 0.0)

    def test_crypto_buy_above_min_notional_approved(self):
        # DOT/USD at $4.00, qty 5.0 -> $20.00 notional >= $10 minimum.
        approved, reason, adjusted = self._buy("DOT/USD", 5.0, 4.00)
        self.assertTrue(approved, reason)
        self.assertGreater(adjusted["quantity"], 0)

    def test_equity_whole_share_floor_still_applies(self):
        # Equities keep the whole-share floor (not the crypto notional floor).
        approved, reason, adjusted = self._buy("AAPL", 0.4, 200.0)
        self.assertFalse(approved)
        self.assertIn("whole share", reason)


class TestMinimumNotionalError(unittest.TestCase):
    def test_message_and_attrs(self):
        err = MinimumNotionalError("DOT/USD", 4.0, 10.0)
        self.assertEqual(err.symbol, "DOT/USD")
        self.assertEqual(err.notional, 4.0)
        self.assertEqual(err.minimum, 10.0)
        self.assertIn("minimum order size", str(err))

    def test_config_default(self):
        self.assertEqual(getattr(config, "MIN_CRYPTO_ORDER_NOTIONAL", None), 10.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)