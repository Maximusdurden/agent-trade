# filename: tests/test_whipsaw_prevention.py
"""Tests for the whipsaw-prevention guardrails (2026-09-09).

Covers:
- Fix 1: VWAP dead zone (BUY/SELL inside ±1σ band rejected; outside approved)
- Fix 2: anti-whipsaw status match (partially_filled recent trade blocks reversal)
- Fix 3: per-ticker daily round-trip budget (force HOLD after budget)
- Fix 5: minimum-edge gate (reversal with < MIN_EDGE_PCT move rejected)
- Fix 6: day-direction lock (reversal without regime change blocked)
- Fix 4: brain prompt includes per-ticker trade history
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config
from core.guardrails import RiskGuardrails


class TestVwapDeadZone(unittest.TestCase):
    """Fix 1: BUY/SELL inside the ±1σ VWAP band is rejected (HOLD)."""

    def setUp(self):
        self.guardrails = RiskGuardrails()
        self._orig_sigma = getattr(config, "VWAP_DEAD_ZONE_SIGMA", 1.0)
        config.VWAP_DEAD_ZONE_SIGMA = 1.0
        self._market = mock.patch.object(RiskGuardrails, "is_market_open_check", return_value=(True, "open"))
        self._market.start()
        # WFC is treated as screener-endorsed so BUY approval tests reach the
        # dead-zone logic instead of being rejected by the strict-universe guard.
        self._watchlist = mock.patch.object(RiskGuardrails, "_in_latest_watchlist", return_value=True)
        self._watchlist.start()

    def tearDown(self):
        config.VWAP_DEAD_ZONE_SIGMA = self._orig_sigma
        self._market.stop()
        self._watchlist.stop()

    def _decision(self, action, price, indicators):
        return {
            "action": action,
            "symbol": "WFC",
            "quantity": 3.0,
            "current_price": price,
            "indicators": indicators,
        }

    def _account(self):
        return {"equity": 100000.0, "cash": 50000.0, "unrealized_pnl": 0.0, "last_equity": 100000.0}

    def _positions(self, owned=3.0):
        return {"WFC": {"qty": owned, "qty_available": owned}}

    def _indicators(self, vwap=89.35, upper=89.60, lower=89.10):
        return {"vwap": vwap, "vwap_upper_1": upper, "vwap_lower_1": lower}

    def test_buy_inside_dead_zone_rejected(self):
        # Price 89.37 is inside [89.10, 89.60] -> HOLD
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("BUY", 89.37, self._indicators()),
            self._account(),
            self._positions(),
        )
        self.assertFalse(approved)
        self.assertIn("VWAP dead zone", msg)
        self.assertEqual(adj["quantity"], 0.0)

    def test_sell_inside_dead_zone_rejected(self):
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("SELL", 89.40, self._indicators()),
            self._account(),
            self._positions(),
        )
        self.assertFalse(approved)
        self.assertIn("VWAP dead zone", msg)

    def test_buy_below_lower_band_allowed(self):
        # Price 88.90 is below vwap_lower_1 (89.10) -> outside dead zone
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("BUY", 88.90, self._indicators()),
            self._account(),
            self._positions(),
        )
        self.assertTrue(approved)

    def test_no_vwap_indicators_skips_dead_zone(self):
        # VWAP gated (None) -> dead zone not applied
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("BUY", 89.37, {"vwap": None, "vwap_upper_1": None, "vwap_lower_1": None}),
            self._account(),
            self._positions(),
        )
        self.assertTrue(approved)


class TestAntiWhipsawStatusMatch(unittest.TestCase):
    """Fix 2: a partially_filled recent trade must block a same-day reversal."""

    def setUp(self):
        self.guardrails = RiskGuardrails()
        self._market = mock.patch.object(RiskGuardrails, "is_market_open_check", return_value=(True, "open"))
        self._market.start()

    def tearDown(self):
        self._market.stop()

    def _decision(self, action, price=89.58):
        return {
            "action": action,
            "symbol": "WFC",
            "quantity": 3.0,
            "current_price": price,
            "indicators": {"vwap": 89.35, "vwap_upper_1": 89.60, "vwap_lower_1": 89.10},
        }

    def _account(self):
        return {"equity": 100000.0, "cash": 50000.0, "unrealized_pnl": 0.0, "last_equity": 100000.0}

    def _positions(self, owned=3.0):
        return {"WFC": {"qty": owned, "qty_available": owned}}

    @mock.patch("core.database.get_recent_trades")
    @mock.patch("core.guardrails.datetime")
    def test_partially_filled_buy_blocks_same_day_sell(self, mock_dt, mock_trades):
        # A partially_filled BUY 25 min ago must block a SELL (anti-whipsaw).
        # Price 89.80 is above the VWAP upper band (89.60) so the dead-zone
        # guardrail doesn't fire first; the anti-whipsaw guard is what rejects.
        # Pin "now" to 16:43Z so the 16:18Z partially_filled buy is 25 min ago.
        from datetime import datetime as _real_dt
        mock_dt.utcnow.return_value = _real_dt(2026, 9, 9, 16, 43, 0)
        # fromisoformat must still parse the trade timestamp.
        mock_dt.fromisoformat.side_effect = _real_dt.fromisoformat
        mock_trades.return_value = [{
            "symbol": "WFC", "status": "partially_filled", "side": "BUY",
            "timestamp": "2026-09-09T16:18:00Z",
        }]
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("SELL", price=89.80),
            self._account(),
            self._positions(),
        )
        self.assertFalse(approved)
        self.assertIn("Anti-whipsaw", msg)


class TestRoundTripBudget(unittest.TestCase):
    """Fix 3: force HOLD once the daily round-trip budget is hit."""

    def setUp(self):
        self.guardrails = RiskGuardrails()
        self._orig = getattr(config, "MAX_ROUND_TRIPS_PER_DAY", 2)
        config.MAX_ROUND_TRIPS_PER_DAY = 2
        self._market = mock.patch.object(RiskGuardrails, "is_market_open_check", return_value=(True, "open"))
        self._market.start()
        self._watchlist = mock.patch.object(RiskGuardrails, "_in_latest_watchlist", return_value=True)
        self._watchlist.start()

    def tearDown(self):
        config.MAX_ROUND_TRIPS_PER_DAY = self._orig
        self._market.stop()
        self._watchlist.stop()

    def _decision(self, action, price=89.37):
        return {
            "action": action,
            "symbol": "WFC",
            "quantity": 3.0,
            "current_price": price,
            "indicators": {"vwap": 89.35, "vwap_upper_1": 89.60, "vwap_lower_1": 89.10},
        }

    def _account(self):
        return {"equity": 100000.0, "cash": 50000.0, "unrealized_pnl": 0.0, "last_equity": 100000.0}

    def _positions(self, owned=3.0):
        return {"WFC": {"qty": owned, "qty_available": owned}}

    @mock.patch("core.database.get_recent_trades")
    @mock.patch("core.feedback.compute_closed_round_trips")
    def test_budget_exhausted_blocks_buy(self, mock_trips, mock_trades):
        # 2 closed round-trips today + 0 open buys = budget hit -> block BUY.
        # Price 88.90 is below the VWAP lower band (89.10) so the dead-zone
        # guardrail doesn't fire first; the round-trip budget guard rejects.
        mock_trips.return_value = [
            {"symbol": "WFC", "close_ts": "2026-09-09T15:00:00Z"},
            {"symbol": "WFC", "close_ts": "2026-09-09T16:00:00Z"},
        ]
        mock_trades.return_value = []
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("BUY", price=88.90),
            self._account(),
            self._positions(),
        )
        self.assertFalse(approved)
        self.assertIn("round-trip budget", msg)


class TestMinEdgeGate(unittest.TestCase):
    """Fix 5: a reversal with < MIN_EDGE_PCT move is rejected."""

    def setUp(self):
        self.guardrails = RiskGuardrails()
        self._orig = getattr(config, "MIN_EDGE_PCT", 0.3)
        config.MIN_EDGE_PCT = 0.3
        self._market = mock.patch.object(RiskGuardrails, "is_market_open_check", return_value=(True, "open"))
        self._market.start()
        self._watchlist = mock.patch.object(RiskGuardrails, "_in_latest_watchlist", return_value=True)
        self._watchlist.start()

    def tearDown(self):
        config.MIN_EDGE_PCT = self._orig
        self._market.stop()
        self._watchlist.stop()

    def _decision(self, action, price):
        return {
            "action": action,
            "symbol": "WFC",
            "quantity": 3.0,
            "current_price": price,
            "indicators": {"vwap": 89.35, "vwap_upper_1": 89.60, "vwap_lower_1": 89.10},
        }

    def _account(self):
        return {"equity": 100000.0, "cash": 50000.0, "unrealized_pnl": 0.0, "last_equity": 100000.0}

    def _positions(self, owned=3.0):
        return {"WFC": {"qty": owned, "qty_available": owned}}

    @mock.patch("core.database.get_recent_trades")
    def test_small_move_reversal_rejected(self, mock_trades):
        # Last fill SELL @ 89.30; now BUY @ 89.05 = 0.28% move < 0.3% -> reject.
        # Price 89.05 is below the VWAP lower band (89.10) so the dead-zone
        # guardrail doesn't fire first; the min-edge guard rejects.
        mock_trades.return_value = [{
            "symbol": "WFC", "side": "SELL", "status": "filled",
            "filled_avg_price": 89.30, "timestamp": "2026-09-09T16:00:00Z",
        }]
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("BUY", 89.05),
            self._account(),
            self._positions(),
        )
        self.assertFalse(approved)
        self.assertIn("Insufficient edge", msg)

    @mock.patch("core.database.get_recent_trades")
    def test_large_move_reversal_allowed(self, mock_trades):
        # Last fill SELL @ 89.30; now BUY @ 90.50 = 1.3% move > 0.3% -> allow.
        mock_trades.return_value = [{
            "symbol": "WFC", "side": "SELL", "status": "filled",
            "filled_avg_price": 89.30, "timestamp": "2026-09-09T16:00:00Z",
        }]
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("BUY", 90.50),
            self._account(),
            self._positions(),
        )
        self.assertTrue(approved)


class TestDayDirectionLock(unittest.TestCase):
    """Fix 6: reversal without a regime change is blocked."""

    def setUp(self):
        self.guardrails = RiskGuardrails()
        self._orig_move = getattr(config, "DAY_DIRECTION_LOCK_MOVE_PCT", 0.5)
        config.DAY_DIRECTION_LOCK_MOVE_PCT = 0.5
        self._market = mock.patch.object(RiskGuardrails, "is_market_open_check", return_value=(True, "open"))
        self._market.start()
        self._watchlist = mock.patch.object(RiskGuardrails, "_in_latest_watchlist", return_value=True)
        self._watchlist.start()

    def tearDown(self):
        config.DAY_DIRECTION_LOCK_MOVE_PCT = self._orig_move
        self._market.stop()
        self._watchlist.stop()

    def _decision(self, action, price):
        return {
            "action": action,
            "symbol": "WFC",
            "quantity": 3.0,
            "current_price": price,
            "indicators": {"vwap": 89.35, "vwap_upper_1": 89.60, "vwap_lower_1": 89.10},
        }

    def _account(self):
        return {"equity": 100000.0, "cash": 50000.0, "unrealized_pnl": 0.0, "last_equity": 100000.0}

    def _positions(self, owned=3.0):
        return {"WFC": {"qty": owned, "qty_available": owned}}

    @mock.patch("core.database.get_recent_trades")
    def test_reversal_without_regime_change_blocked(self, mock_trades):
        # Last fill BUY @ 89.70 (above upper band 89.60); now SELL @ 89.99.
        # Move = 0.32% (passes min-edge 0.3%) but < 0.5% (fails day-direction),
        # and no VWAP band is crossed (both fills above the band) -> lock fires.
        mock_trades.return_value = [{
            "symbol": "WFC", "side": "BUY", "status": "filled",
            "filled_avg_price": 89.70, "timestamp": "2026-09-09T16:00:00Z",
        }]
        approved, msg, adj = self.guardrails.validate_and_adjust_decision(
            self._decision("SELL", 89.99),
            self._account(),
            self._positions(),
        )
        self.assertFalse(approved)
        self.assertIn("Day-direction lock", msg)


class TestBrainTradeMemory(unittest.TestCase):
    """Fix 4: the brain prompt includes per-ticker trade history."""

    def test_prompt_contains_trade_history(self):
        from core.trading_brain import TradingBrain
        brain = TradingBrain.__new__(TradingBrain)  # bypass __init__ (no LLM needed)
        brain.is_mock = True
        brain.provider = "mock"
        brain.llm_client = None

        market_data = [{
            "symbol": "WFC",
            "current_price": 89.37,
            "daily_return_pct": 0.001,
            "indicators": {"rsi_14": 56.0, "vwap": 89.35},
            "advanced_pivots": {},
            "news": [],
        }]
        account = {"equity": 100000.0, "cash": 50000.0, "unrealized_pnl": 0.0}
        positions = {"WFC": {"qty": 3.0, "market_value": 268.0, "avg_entry_price": 89.30, "unrealized_pnl": 0.2}}

        with mock.patch("core.trading_brain.database.get_recent_trades_by_symbol") as mock_hist, \
             mock.patch("core.trading_brain.database.get_active_strategy", return_value="rule"), \
             mock.patch("core.trading_brain.database.get_active_strategy_hint", return_value=None), \
             mock.patch("core.trading_brain.database.get_performance_summary", return_value={"text_summary": "ok"}):
            mock_hist.return_value = [
                {"timestamp": "2026-09-09T15:51:00Z", "symbol": "WFC", "side": "buy",
                 "qty": 3.0, "filled_avg_price": 89.37, "status": "filled"},
                {"timestamp": "2026-09-09T16:43:00Z", "symbol": "WFC", "side": "sell",
                 "qty": 3.0, "filled_avg_price": 89.58, "status": "filled"},
            ]
            prompt = brain._build_prompt(market_data, account, positions, [])
            self.assertIn("RECENT TRADES", prompt)
            self.assertIn("15:51 BUY 3.0 @ $89.37", prompt)
            self.assertIn("16:43 SELL 3.0 @ $89.58", prompt)


if __name__ == "__main__":
    unittest.main()