import os
import sys
import unittest

os.environ["DATABASE_FILENAME"] = "test_anti_scale_in.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config
from core.database import init_db, get_db_connection
from core.guardrails import RiskGuardrails


def _clean():
    with get_db_connection() as conn:
        for t in ("watchlist_history",):
            try:
                conn.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        conn.commit()


def _set_watchlist(syms):
    import json
    with get_db_connection() as conn:
        conn.execute("INSERT INTO watchlist_history (timestamp, watchlist) VALUES (?, ?)",
                     ("2026-08-01 12:00:00", json.dumps(syms)))
        conn.commit()


class TestAntiScaleIn(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()
        # Clear the process-global feedback FIFO memo cache so round-trips computed
        # on a DIFFERENT test DB by an earlier module don't trip the circuit breaker.
        import core.feedback as fb
        fb._memo.clear()
        self.g = RiskGuardrails()

    def _buy(self, symbol, positions, price=100.0, qty=1.0):
        decision = {"action": "BUY", "symbol": symbol, "quantity": qty,
                    "current_price": price, "conviction": 0.8,
                    "direction": "bullish", "instrument": "stock"}
        self.g.is_market_open_check = lambda: (True, "open")  # tests not time-dependent
        return self.g.validate_and_adjust_decision(
            decision, {"equity": 100000.0, "cash": 50000.0, "unrealized_pnl": 0.0},
            positions,
        )

    def test_avg_down_unwatched_blocked(self):
        _set_watchlist(["MSFT"])
        pos = {"MS": {"qty": 100, "avg_entry_price": 213.0}}
        ok, msg, _ = self._buy("MS", pos, price=210.0)
        self.assertFalse(ok, f"Avg-down unwatched should be blocked: {msg}")
        # Blocked by strict-universe OR anti-scale-in (either is a correct block).
        self.assertTrue("Strict-universe" in msg or "Anti-scale-in" in msg, msg)

    def test_avg_down_watched_blocked(self):
        # MS IS in watchlist but still averaging down -> anti-scale-in nets it.
        _set_watchlist(["MS", "MSFT"])
        pos = {"MS": {"qty": 100, "avg_entry_price": 213.0}}
        ok, msg, _ = self._buy("MS", pos, price=207.0)
        self.assertFalse(ok, f"Avg-down watched should still be blocked by anti-scale-in: {msg}")
        self.assertIn("Anti-scale-in", msg)

    def test_add_to_watched_above_entry_allowed(self):
        # MS is watched AND above entry -> not averaging down -> allowed to add.
        _set_watchlist(["MS"])
        pos = {"MS": {"qty": 100, "avg_entry_price": 210.0}}
        ok, msg, _ = self._buy("MS", pos, price=213.0)  # above avg -> not averaged down
        self.assertTrue(ok, f"Add above entry (not averaging down) should be allowed: {msg}")

    def test_crypto_ignores_scale_in(self):
        _set_watchlist(["MSFT"])
        pos = {"SOL/USD": {"qty": 10, "avg_entry_price": 150.0}}
        ok, msg, _ = self._buy("SOL/USD", pos, price=145.0)
        self.assertTrue(ok, f"Crypto avg-down should not be blocked: {msg}")


if __name__ == "__main__":
    unittest.main(verbosity=2)