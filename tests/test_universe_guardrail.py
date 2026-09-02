import os
import sys
import unittest

os.environ["DATABASE_FILENAME"] = "test_universe_guardrail.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config
from core.database import init_db, get_db_connection
from core.guardrails import RiskGuardrails

def _clean():
    with get_db_connection() as conn:
        for t in ("watchlist_history", "trades"):
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

class TestStrictUniverseGuardrail(unittest.TestCase):
    def setUp(self):
        init_db()
        _clean()
        # Clear the process-global feedback FIFO memo cache so round-trips computed
        # on a DIFFERENT test DB by an earlier test module don't leak in and trip
        # the circuit breaker for symbols we test here (e.g. SOL/USD).
        import core.feedback as fb
        fb._memo.clear()

    def _buy(self, symbol, qty=1.0, price=100.0):
        decision = {"action": "BUY", "symbol": symbol, "quantity": qty,
                    "current_price": price, "conviction": 0.8,
                    "direction": "bullish", "instrument": "stock"}
        guardrails = RiskGuardrails()
        return guardrails.validate_and_adjust_decision(
            decision,
            {"equity": 100000.0, "cash": 50000.0, "unrealized_pnl": 0.0},
            {},
        )

    def test_watched_symbol_buys(self):
        _set_watchlist(["MSFT"])
        ok, msg, adj = self._buy("MSFT")
        self.assertTrue(ok, f"MSFT should be allowed (in watchlist): {msg}")

    def test_untracked_equity_blocked(self):
        # SPY in static TRADING_UNIVERSE but never in watchlist -> blocked
        _set_watchlist(["MSFT"])
        ok, msg, adj = self._buy("SPY")
        self.assertFalse(ok, f"SPY should be blocked (not in watchlist): {msg}")
        self.assertIn("Strict-universe", msg)

    def test_crypto_allowed(self):
        _set_watchlist(["MSFT"])
        ok, msg, adj = self._buy("SOL/USD")
        self.assertTrue(ok, f"Crypto should be allowed 24/7: {msg}")

    def test_empty_watchlist_blocks_equity(self):
        _set_watchlist([])
        ok, msg, adj = self._buy("NVDA")
        self.assertFalse(ok, f"NVDA should be blocked when watchlist empty (equity): {msg}")


if __name__ == "__main__":
    unittest.main(verbosity=2)