# filename: tests/test_cluster_guardrail.py
"""Tests for the correlation/concentration cluster guardrail.

Verifies that a BUY whose proposed value, when added to the existing correlated
cluster exposure, would exceed MAX_CLUSTER_ALLOCATION_PCT of equity is either
scaled down or rejected entirely — so the portfolio can't become an oversized,
undiversified bet on one correlated theme.
"""
import os
import sys
import unittest

# Use a SEPARATE test database so the strict-universe / circuit-breaker guardrails
# never see round-trips leaked from other test modules (which run on the shared
# default DB), and so cluster tests stay isolated.
os.environ["DATABASE_FILENAME"] = "test_cluster_guardrail.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config
from core.guardrails import RiskGuardrails
from core.strategy_rules import build_symbol_to_cluster, normalize_symbol


class TestClusterMapping(unittest.TestCase):
    def test_mapping_normalizes_and_flattens(self):
        clusters = {
            "CRYPTO": ["BTC/USD", "SOL/USD", "btc/usd"],
            "TECH": ["NVDA", "AMD"],
        }
        mapping = build_symbol_to_cluster(clusters)
        self.assertEqual(mapping["BTC/USD"], "CRYPTO")
        self.assertEqual(mapping["SOL/USD"], "CRYPTO")
        self.assertEqual(mapping["NVDA"], "TECH")
        self.assertEqual(mapping["AMD"], "TECH")
        # Unlisted symbols are not mapped.
        self.assertNotIn("AAPL", mapping)

    def test_normalize_crypto_form(self):
        self.assertEqual(normalize_symbol("SOLUSD"), "SOL/USD")
        self.assertEqual(normalize_symbol("SOL/USD"), "SOL/USD")

    def test_each_config_symbol_maps_to_one_cluster(self):
        # Belt-and-braces: every symbol in CORRELATION_CLUSTERS must map to exactly
        # one cluster (no duplicate membership ambiguity).
        from core.config import CORRELATION_CLUSTERS
        mapping = build_symbol_to_cluster(CORRELATION_CLUSTERS)
        self.assertEqual(
            len(mapping),
            sum(len(list(dict.fromkeys(m))) for m in CORRELATION_CLUSTERS.values()),
        )
        self.assertIn("NVDA", mapping)
        self.assertIn("BTC/USD", mapping)


class TestClusterGuardrail(unittest.TestCase):
    def setUp(self):
        # Isolate from the shared default DB: the strict-universe + circuit-breaker
        # guardrails read `database.get_latest_watchlist_raw()` / closed round-trips.
        # Point DATABASE_PATH at a throwaway file, clear it, AND clear the feedback
        # FIFO memo cache (a 60s process-wide cache) so the circuit breaker never
        # sees SOL/USD whipsaw round-trips computed by a prior test module.
        from core import config as _c, database as _db
        import core.feedback as _fb
        _fb._memo.clear()  # drop the 60s process-wide FIFO cache from prior modules
        import tempfile
        self._tmpdb = tempfile.mktemp(suffix=".db")
        _c.DATABASE_PATH = self._tmpdb
        _db.init_db()
        with _db.get_db_connection() as conn:
            conn.execute("DELETE FROM trades")
            conn.execute("DELETE FROM watchlist_history")
            conn.commit()
        self.guardrails = RiskGuardrails()

    def tearDown(self):
        try:
            import os as _os
            _os.remove(self._tmpdb)
        except Exception:
            pass

    def _account(self, equity=100000.0):
        return {"equity": equity, "cash": 60000.0, "unrealized_pnl": 0.0, "last_equity": equity}

    def _decision(self, symbol, qty, price):
        return {
            "action": "BUY",
            "symbol": symbol,
            "quantity": qty,
            "current_price": price,
            "conviction": 0.7,
            "direction": "bullish",
        }

    def _position(self, qty, price):
        return {"qty": qty, "qty_available": qty, "market_value": qty * price,
                "avg_entry_price": price, "unrealized_pnl": 0.0}

    def test_buy_under_cluster_limit_approved(self):
        # Cluster cap default 40% of $100k = $40k. Holding $20k BTC; buy $10k SOL.
        positions = {"BTC/USD": self._position(0.25, 80000)}  # $20k
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("SOL/USD", qty=250, price=40),  # $10k
            self._account(),
            positions,
        )
        self.assertTrue(approved, msg)
        self.assertEqual(adj["quantity"], 250)

    def test_buy_over_cluster_limit_scaled_down(self):
        # Cluster cap = $40k. Holding $38k BTC; proposed $10k SOL -> overlap, scale to $2k.
        cap = config.MAX_CLUSTER_ALLOCATION_PCT * 100000.0  # $40k
        positions = {"BTC/USD": self._position(0.475, 80000)}  # $38k
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("SOL/USD", qty=250, price=40),  # $10k proposed
            self._account(),
            positions,
        )
        self.assertTrue(approved, msg)
        # Remaining room = cap - existing = $2k -> qty = 2000/40 = 50
        expected_qty = round((cap - 38000) / 40, 4)
        self.assertAlmostEqual(adj["quantity"], expected_qty, places=2)

    def test_buy_no_room_in_cluster_rejected(self):
        # Cluster cap = $40k. Already at/above cap from existing crypto positions.
        positions = {"BTC/USD": self._position(0.5, 80000)}  # $40k exactly
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("SOL/USD", qty=100, price=40),  # $4k proposed
            self._account(),
            positions,
        )
        self.assertFalse(approved)
        self.assertEqual(adj["quantity"], 0.0)
        self.assertIn("CRYPTO", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)