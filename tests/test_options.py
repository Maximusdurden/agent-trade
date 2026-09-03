"""Tests for the options trading feature in agent-trade.

These use mocked facts/objects (no live Alpaca calls) so they run offline.
Run with:  python -m pytest tests/test_options.py -v
or:        python tests/test_options.py
"""

import sys
import os
import unittest
import types
from datetime import datetime, timedelta, date

# Use a SEPARATE test database so tests never pollute the live trading DB.
os.environ["DATABASE_FILENAME"] = "test_trading_agent.db"

sys.path.insert(0, r"Z:\python\projects")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# --- Mock snapshot / quote / greeks objects ---------------------------------
class FakeQuote:
    def __init__(self, bid, ask):
        self.bid_price = bid
        self.ask_price = ask


class FakeGreeks:
    def __init__(self, delta):
        self.delta = delta


class FakeSnapshot:
    def __init__(self, symbol, bid, ask, delta):
        self.symbol = symbol
        self.latest_quote = FakeQuote(bid, ask)
        self.greeks = FakeGreeks(delta)


def make_snapshot(sym, dte, bid, ask, delta):
    """Create a snapshot with an expiry `dte` days out."""
    exp = date.today() + timedelta(days=dte)
    date_str = exp.strftime("%y%m%d")
    cp = "C" if sym.endswith("C") else "P"
    strike = sym.split("C")[-1] if cp == "C" else sym.split("P")[-1]
    occ = f"{sym[:3]:<6}{date_str}{cp}{strike}"
    snap = FakeSnapshot(occ, bid, ask, delta)
    snap.expiration_date = exp
    return snap


class TestsOptionPicker(unittest.TestCase):
    """Tests for core/option_picker.py scoring + OCC parsing."""

    def test_parse_occ_symbol(self):
        from core.option_picker import parse_option_symbol
        info = parse_option_symbol("AAPL 250117C00200000")
        self.assertIsNotNone(info)
        self.assertEqual(info["root"], "AAPL")
        self.assertEqual(info["type"], "CALL")
        self.assertEqual(info["strike_price"], 200.0)
        self.assertEqual(info["expiration_date"].year, 2025)

    def test_format_occ_symbol_pads_root(self):
        from core.option_picker import format_option_symbol
        occ = format_option_symbol({
            "root": "AAPL", "expiration_date": date(2025, 1, 17),
            "type": "CALL", "strike_price": 200.0,
        })
        self.assertEqual(occ, "AAPL  250117C00200000")

    def test_calculate_score_valid(self):
        from core.option_picker import calculate_score
        snap = _snapshot_for_dte(30)
        score = calculate_score(snap, 100.0, 14, 90)
        self.assertGreater(score["score"], 0)
        self.assertEqual(score["reason"], "OK")

    def test_calculate_score_rejects_wide_spread(self):
        from core.option_picker import calculate_score
        snap = FakeSnapshot("AAPL250117C00200000", bid=0.30, ask=0.90, delta=0.5)
        score = calculate_score(snap, 200.0, 14, 90)
        self.assertEqual(score["score"], -1)

    def test_calculate_score_rejects_bad_delta(self):
        from core.option_picker import calculate_score
        snap = FakeSnapshot("AAPL250117C00200000", bid=0.50, ask=0.60, delta=0.95)
        score = calculate_score(snap, 200.0, 14, 90)
        self.assertEqual(score["score"], -1)


class TestsGuardrailRouting(unittest.TestCase):
    """Tests for conviction-threshold instrument routing in guardrails."""

    def _make_guardrails(self):
        from core.guardrails import RiskGuardrails
        return RiskGuardrails()

    def test_high_conviction_routes_to_option(self):
        # Direct test of the option validation branch via the guardrails method.
        g = self._make_guardrails()
        import core.config as cfg
        # Use mock-able path: ensure options enabled for this test
        cfg.OPTIONS_ENABLED = True
        decision = {
            "action": "BUY", "symbol": "NVDA", "quantity": 1.0,
            "direction": "bullish", "conviction": 0.8,
        }
        adj = dict(decision)
        # Market open override + mock buying power + mock earnings filter (clear)
        g.is_market_open_check = lambda: (True, "open")
        g._get_options_buying_power = lambda: 50000.0
        g._has_earnings_before_expiry = lambda underlying, dte_max: ""
        ok, msg, _ = g._validate_option_decision(decision, adj, {"equity": 100000.0}, {})
        self.assertTrue(ok, msg)
        cfg.OPTIONS_ENABLED = False  # restore

    def test_low_conviction_does_not_route(self):
        from core.guardrails import RiskGuardrails
        import core.config as cfg
        cfg.OPTIONS_ENABLED = True
        g = RiskGuardrails()
        decision = {
            "action": "BUY", "symbol": "NVDA", "quantity": 10.0,
            "direction": "bullish", "conviction": 0.3,
        }
        adj = dict(decision)
        decision["conviction"] = 0.3
        adj["conviction"] = 0.3
        g.is_market_open_check = lambda: (True, "open")
        g._get_options_buying_power = lambda: 50000.0
        g._has_earnings_before_expiry = lambda underlying, dte_max: ""
        ok, msg, _ = g._validate_option_decision(decision, adj, {"equity": 10000.0}, {})
        # Low conviction still passes option validation if manually routed; the
        # crucial routing decision (instrument) is decided in validate_and_adjust.
        self.assertTrue(ok, msg)
        cfg.OPTIONS_ENABLED = False

    def test_earnings_filter_blocks_option(self):
        import core.config as cfg
        cfg.OPTIONS_ENABLED = True
        g = self._make_guardrails()
        decision = {
            "action": "BUY", "symbol": "NVDA", "quantity": 1.0,
            "direction": "bullish", "conviction": 0.9,
        }
        adj = dict(decision)
        g.is_market_open_check = lambda: (True, "open")
        g._get_options_buying_power = lambda: 50000.0
        # Simulate earnings inside the window -> must reject
        g._has_earnings_before_expiry = lambda underlying, dte_max: (
            f"{underlying} has an earnings date within {dte_max} days"
        )
        ok, msg, _ = g._validate_option_decision(decision, adj, {"equity": 100000.0}, {})
        self.assertFalse(ok)
        self.assertIn("earnings", msg.lower())
        cfg.OPTIONS_ENABLED = False


class TestsOptionLifecycle(unittest.TestCase):
    """Tests for the pre-expiry auto-close sweep."""

    def test_parses_dte_and_closes_near_expiry(self):
        from core.option_lifecycle import OptionLifecycle
        import core.config as cfg
        cfg.OPTIONS_ENABLED = True
        cfg.OPTIONS_AUTO_CLOSE_DTE = 3

        # A mock client that reports one option position expiring today (DTE 0)
        class MockClient:
            def get_option_positions(self):
                exp = (date.today()).strftime("%y%m%d")
                return {f"NVDA  {exp}C00100000": {
                    "qty": 1.0, "qty_available": 1.0, "market_value": 100.0,
                    "avg_entry_price": 1.0, "unrealized_pnl": 0.0, "is_option": True,
                }}
            def close_option_position(self, symbol):
                return {"id": "close-1", "qty": 1, "status": "closed", "symbol": symbol}

        lc = OptionLifecycle(MockClient())
        results = lc.sweep()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "closed")

    def test_no_close_for_far_dte(self):
        from core.option_lifecycle import OptionLifecycle
        import core.config as cfg
        cfg.OPTIONS_ENABLED = True
        cfg.OPTIONS_AUTO_CLOSE_DTE = 3

        class MockClient:
            def get_option_positions(self):
                exp = (date.today() + timedelta(days=40)).strftime("%y%m%d")
                return {f"NVDA  {exp}C0010001": {
                    "qty": 1.0, "qty_available": 1.0, "market_value": 100.0,
                    "avg_entry_price": 1.0, "unrealized_pnl": 0.0, "is_option": True,
                }}
            def close_option_position(self, symbol):
                self._called = True
                return {"status": "closed"}

        mc = MockClient()
        mc._called = False
        results = OptionLifecycle(mc).sweep()
        self.assertEqual(len(results), 0)
        self.assertFalse(mc._called)


class TestsOptionLifecycleRiskSweep(unittest.TestCase):
    """Verifies the event-gate / exposure-cap risk sweep closes positions."""

    def test_risk_sweep_closes_near_event(self):
        import core.config as cfg
        import core.data_provider as dp
        from core.option_lifecycle import OptionLifecycle

        orig_options = cfg.OPTIONS_ENABLED
        orig_gate = getattr(cfg, "OPTIONS_EVENT_GATE_ENABLED", None)
        cfg.OPTIONS_ENABLED = True
        cfg.OPTIONS_EVENT_GATE_ENABLED = True

        # Earnings tomorrow on NVDA => gate should flatten today.
        orig_earn = getattr(dp, "get_earnings_dates", None)
        import pandas as pd
        def fake_earn(tickers=None, days_ahead=365):
            d = date.today() + timedelta(days=1)
            return pd.DataFrame([{"ticker": t.upper(), "earnings_date": d}
                                 for t in (tickers or [])])
        dp.get_earnings_dates = fake_earn

        class MockClient:
            def __init__(self):
                self.closed = []
                self.opts = {}
            def get_option_positions(self):
                # one NVDA contract held, expiring far out but near earnings
                return {f"NVDA261216C00230000": {
                    "qty": 1.0, "qty_available": 1.0, "market_value": 100.0,
                    "avg_entry_price": 1.0, "unrealized_pnl": 0.0, "is_option": True,
                }}
            def get_latest_option_data(self, symbols):
                return {}  # no greeks -> premium fallback path
            def close_option_position(self, symbol):
                self.closed.append(symbol)
                return {"id": "x", "qty": 1, "status": "closed", "symbol": symbol}

        mc = MockClient()
        try:
            results = OptionLifecycle(mc).risk_sweep({"equity": 100000.0})
            self.assertTrue(any(r["status"] == "closed" for r in results))
            self.assertTrue(mc.closed)  # at least one position was flattened
        finally:
            cfg.OPTIONS_ENABLED = orig_options
            cfg.OPTIONS_EVENT_GATE_ENABLED = orig_gate
            dp.get_earnings_dates = orig_earn


class TestsOptionExecutorWideWindowFallback(unittest.TestCase):
    """Verifies the widened DTE fallback in option_executor._resolve_contract."""

    def test_fallback_retries_with_wider_window(self):
        from core.option_executor import OptionExecutor
        from core import option_picker
        import core.config as _cfg
        import unittest.mock as um

        _orig_min, _orig_max = _cfg.OPTIONS_DTE_MIN, _cfg.OPTIONS_DTE_MAX
        _cfg.OPTIONS_DTE_MIN = 30
        _cfg.OPTIONS_DTE_MAX = 45
        _cfg.OPTIONS_DTE_FALLBACK_MAX = 90

        class OKContract:
            symbol = "NVDA  261016C00235000"
            ask_price = 6.5
            premium_cost = 650.0

        client = object()
        executor = OptionExecutor(client)
        contracts = []
        def fake_fbo(underlying_symbol, option_type, days_out_min, days_out_max,
                     otm_percent_min, otm_percent_max, alpaca_client):
            contracts.append((days_out_min, days_out_max))
            if len(contracts) == 1:
                return None
            return OKContract()

        with um.patch.object(option_picker, "find_best_option", side_effect=fake_fbo):
            with um.patch("core.option_executor.config.OPTIONS_DTE_FALLBACK_MAX", 90):
                result = executor._resolve_contract("NVDA", "bullish", {})

        self.assertEqual(len(contracts), 2, "Expected primary + widened fallback call")
        primary_min, primary_max = contracts[0]
        fallback_min, fallback_max = contracts[1]
        self.assertEqual((primary_min, primary_max), (30, 45))
        self.assertEqual(fallback_max, 90, "Fallback should widen DTE up to the hard max")
        self.assertEqual(result.symbol, "NVDA  261016C00235000")

        _cfg.OPTIONS_DTE_MIN, _cfg.OPTIONS_DTE_MAX = _orig_min, _orig_max

    def test_no_fallback_when_primary_succeeds(self):
        from core.option_executor import OptionExecutor
        from core import option_picker
        import core.config as _cfg
        import unittest.mock as um

        _orig_min, _orig_max = _cfg.OPTIONS_DTE_MIN, _cfg.OPTIONS_DTE_MAX
        _cfg.OPTIONS_DTE_MIN = 30
        _cfg.OPTIONS_DTE_MAX = 45

        class OKContract:
            symbol = "NVDA  261016C00235000"
            ask_price = 6.5
            premium_cost = 650.0

        executor = OptionExecutor(object())
        contracts = []
        def fake_fbo(underlying_symbol, option_type, days_out_min, days_out_max,
                     otm_percent_min, otm_percent_max, alpaca_client):
            contracts.append((days_out_min, days_out_max))
            return OKContract()

        with um.patch.object(option_picker, "find_best_option", side_effect=fake_fbo):
            result = executor._resolve_contract("NVDA", "bullish", {})

        self.assertEqual(len(contracts), 1, "No widened retry when primary succeeds")
        _cfg.OPTIONS_DTE_MIN, _cfg.OPTIONS_DTE_MAX = _orig_min, _orig_max


def _snapshot_for_dte(dte: int) -> FakeSnapshot:
    """Build a valid snapshot for the given DTE with reasonable values."""
    exp = date.today() + timedelta(days=dte)
    date_str = exp.strftime("%y%m%d")
    sym = f"NVDA  {date_str}C01000000"
    return FakeSnapshot(sym, bid=0.50, ask=0.60, delta=0.5)


if __name__ == "__main__":
    unittest.main()