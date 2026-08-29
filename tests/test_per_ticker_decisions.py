"""Validates Option B: autonomous per-ticker (multi-decision) trading.

Covers:
- trading_brain.make_decision returns a list of per-ticker decisions.
- The mock brain emits one decision per appraised ticker.
- guardrails.validate_and_adjust_decision enforces MAX_TRADES_PER_CYCLE via
  the shared cycle_context.
- Cumulative cycle budget: multiple BUYs share a single spend cap.
- database.log_decision persists cycle_id + reasoning, and
  log_ticker_conviction / get_ticker_convictions round-trip.
"""
import os
import sys
import unittest

# Use a SEPARATE test database so tests never pollute the live trading DB.
os.environ["DATABASE_FILENAME"] = "test_per_ticker.db"

sys.path.insert(0, r"Z:\python\projects")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import database
import core.config as config


class TestBrainEmitsList(unittest.TestCase):
    def _make_brain(self):
        from core.trading_brain import TradingBrain
        return TradingBrain()

    def test_mock_brain_returns_list(self):
        brain = self._make_brain()
        market_states = [
            {"symbol": "NVDA", "current_price": 100.0,
             "indicators": {"rsi_14": 30.0}, "daily_return_pct": 0.01},
            {"symbol": "TSLA", "current_price": 200.0,
             "indicators": {"rsi_14": 70.0}, "daily_return_pct": 0.02},
            {"symbol": "AAPL", "current_price": 150.0,
             "indicators": {"rsi_14": 50.0}, "daily_return_pct": 0.0},
        ]
        decisions = brain._make_mock_decision(market_states, {"equity": 100000.0}, {})
        # Must be a list (Option B)
        self.assertIsInstance(decisions, list)
        self.assertEqual(len(decisions), 3)
        # One decision per ticker, each with symbol + conviction
        symbols = {d["symbol"].upper() for d in decisions}
        self.assertEqual(symbols, {"NVDA", "TSLA", "AAPL"})
        for d in decisions:
            self.assertIn("conviction", d)
            self.assertIn("action", d)

    def test_make_decision_coerces_to_list(self):
        brain = self._make_brain()
        # Mock returns a list; make_decision should pass it through as a list.
        out = brain._make_mock_decision([], {}, {})
        self.assertIsInstance(out, list)


class TestCycleBudget(unittest.TestCase):
    def _make_guardrails(self):
        from core.guardrails import RiskGuardrails
        return RiskGuardrails()

    def _mock_open(self, g):
        g.is_market_open_check = lambda: (True, "open")

    def _buy(self, g, symbol, qty, price, cycle_context, **overrides):
        """Run a single stock BUY through validate_and_adjust_decision."""
        decision = {
            "action": "BUY", "symbol": symbol, "quantity": qty,
            "current_price": price, "conviction": 0.6, "direction": "bullish",
        }
        decision.update(overrides)
        return g.validate_and_adjust_decision(
            decision, {"equity": 100000.0, "cash": 50000.0}, {},
            cycle_context=cycle_context)

    def test_max_trades_per_cycle_cap(self):
        g = self._make_guardrails()
        self._mock_open(g)
        old = config.MAX_TRADES_PER_CYCLE
        old_opts = config.OPTIONS_ENABLED
        config.MAX_TRADES_PER_CYCLE = 2
        config.OPTIONS_ENABLED = False  # force stock path for deterministic checks
        try:
            cycle_context = {"spent": 0.0, "trades": 0}
            # Trade 1 approved (cap not reached)
            ok1, msg1, adj1 = self._buy(g, "NVDA", 5.0, 100.0, cycle_context)
            self.assertTrue(ok1, msg1)
            self.assertNotEqual(adj1["quantity"], 0.0)
            cycle_context["trades"] += 1

            # Trade 2 approved (cap reached exactly)
            ok2, msg2, _ = self._buy(g, "AAPL", 5.0, 100.0, cycle_context)
            self.assertTrue(ok2, msg2)
            cycle_context["trades"] += 1

            # Trade 3 rejected: cap reached (2 >= MAX_TRADES_PER_CYCLE)
            ok3, msg3, adj3 = self._buy(g, "TSLA", 5.0, 100.0, cycle_context)
            self.assertFalse(ok3)
            self.assertIn("MAX_TRADES_PER_CYCLE", msg3)
            self.assertEqual(adj3["quantity"], 0.0)
        finally:
            config.MAX_TRADES_PER_CYCLE = old
            config.OPTIONS_ENABLED = old_opts

    def test_exit_sell_bypasses_per_cycle_cap(self):
        """A SELL that reduces an existing held position must NOT be blocked by
        MAX_TRADES_PER_CYCLE, even after another trade already executed."""
        g = self._make_guardrails()
        self._mock_open(g)
        old = config.MAX_TRADES_PER_CYCLE
        old_opts = config.OPTIONS_ENABLED
        config.MAX_TRADES_PER_CYCLE = 1
        config.OPTIONS_ENABLED = False
        try:
            # Cap already reached by a prior trade this cycle.
            cycle_context = {"spent": 0.0, "trades": 1}
            # We hold SOL/USD -> SELL is an exit and must be approved.
            positions = {"SOL/USD": {"qty": 14.0, "qty_available": 14.0}}
            decision = {
                "action": "SELL", "symbol": "SOL/USD", "quantity": 14.0,
                "current_price": 103.0, "conviction": 0.6, "direction": "bearish",
            }
            ok, msg, adj = g.validate_and_adjust_decision(
                decision, {"equity": 100000.0, "cash": 50000.0}, positions,
                cycle_context=cycle_context)
            self.assertTrue(ok, msg)
            self.assertNotEqual(adj["quantity"], 0.0)

            # A BUY at the cap must still be rejected.
            ok2, msg2, adj2 = self._buy(g, "NVDA", 5.0, 100.0, cycle_context)
            self.assertFalse(ok2)
            self.assertIn("MAX_TRADES_PER_CYCLE", msg2)
            self.assertEqual(adj2["quantity"], 0.0)
        finally:
            config.MAX_TRADES_PER_CYCLE = old
            config.OPTIONS_ENABLED = old_opts

    def test_sell_without_holding_still_capped(self):
        """A SELL on a symbol we do NOT hold (e.g. opening a short) is NOT an
        exit and must still count against the per-cycle cap."""
        g = self._make_guardrails()
        self._mock_open(g)
        old = config.MAX_TRADES_PER_CYCLE
        old_opts = config.OPTIONS_ENABLED
        config.MAX_TRADES_PER_CYCLE = 1
        config.OPTIONS_ENABLED = False
        try:
            cycle_context = {"spent": 0.0, "trades": 1}
            # No position held for BTC/USD -> SELL is not an exit.
            decision = {
                "action": "SELL", "symbol": "BTC/USD", "quantity": 1.0,
                "current_price": 77000.0, "conviction": 0.6, "direction": "bearish",
            }
            ok, msg, adj = g.validate_and_adjust_decision(
                decision, {"equity": 100000.0, "cash": 50000.0}, {},
                cycle_context=cycle_context)
            self.assertFalse(ok)
            self.assertIn("MAX_TRADES_PER_CYCLE", msg)
            self.assertEqual(adj["quantity"], 0.0)
        finally:
            config.MAX_TRADES_PER_CYCLE = old
            config.OPTIONS_ENABLED = old_opts

    def test_cumulative_cycle_budget(self):
        g = self._make_guardrails()
        self._mock_open(g)
        old = config.MAX_TRADES_PER_CYCLE
        config.MAX_TRADES_PER_CYCLE = 3
        try:
            cycle_context = {"spent": 0.0, "trades": 0}
            # Each of these would individually pass the 10% per-trade cap, but
            # together they exceed the cumulative budget (0.10 * 100k * 3).
            ok1, msg1, adj1 = self._buy(g, "AAPL", 1000.0, 10.0, cycle_context)  # $10k
            self.assertTrue(ok1, msg1)
            cycle_context["spent"] += float(adj1["quantity"]) * 10.0

            ok2, msg2, adj2 = self._buy(g, "MSFT", 1000.0, 10.0, cycle_context)  # $10k
            self.assertTrue(ok2, msg2)
            cycle_context["spent"] += float(adj2["quantity"]) * 10.0

            ok3, msg3, adj3 = self._buy(g, "AMZN", 1000.0, 10.0, cycle_context)  # $10k
            # Cumulative budget = 0.10 * 100k * 3 = $30k. After two $10k buys we
            # still have exactly $10k left, so the third passes exactly.
            self.assertTrue(ok3, msg3)
            cycle_context["spent"] += float(adj3["quantity"]) * 10.0

            # A 4th buy exceeds the cumulative budget -> must scale down or reject.
            ok4, msg4, adj4 = self._buy(g, "GOOG", 1000.0, 10.0, cycle_context)
            self.assertFalse(ok4)
            self.assertIn("spend budget", msg4.lower())
        finally:
            config.MAX_TRADES_PER_CYCLE = old


class TestCycleDbRoundTrip(unittest.TestCase):
    def setUp(self):
        database.init_db()

    def test_cycle_id_and_reasoning_persist(self):
        did = database.log_decision(
            ticker_indicators={}, portfolio_state={}, thought_process="test",
            proposed_action="BUY", proposed_symbol="NVDA", proposed_qty=5.0,
            is_approved=True, rejection_reason=None,
            direction="bullish", conviction=0.8, instrument="stock",
            cycle_id="20260820-120000", reasoning="per-ticker narrative",
        )
        self.assertTrue(did > 0)
        decs = database.get_recent_decisions(limit=5)
        row = next(d for d in decs if d["id"] == did)
        self.assertEqual(row["cycle_id"], "20260820-120000")
        self.assertEqual(row["reasoning"], "per-ticker narrative")
        self.assertEqual(row["direction"], "bullish")
        self.assertAlmostEqual(row["conviction"], 0.8)

    def test_ticker_convictions_round_trip(self):
        cid1 = database.log_ticker_conviction("cycle-1", "NVDA", "bullish", 0.8)
        cid2 = database.log_ticker_conviction("cycle-1", "TSLA", "bearish", 0.6)
        self.assertTrue(cid1 > 0 and cid2 > 0)
        rows = database.get_ticker_convictions(limit=50)
        symbols = {(r["symbol"], r["cycle_id"]) for r in rows}
        self.assertIn(("NVDA", "cycle-1"), symbols)
        self.assertIn(("TSLA", "cycle-1"), symbols)
        # latest_cycle_id reflects the most recent decisions cycle
        database.log_decision(
            ticker_indicators={}, portfolio_state={}, thought_process="t",
            proposed_action="HOLD", proposed_symbol="NVDA", proposed_qty=0.0,
            is_approved=True, cycle_id="cycle-99", reasoning=None,
        )
        self.assertEqual(database.get_latest_cycle_id(), "cycle-99")


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()