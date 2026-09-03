"""Tests for core/option_risk.py — the options event gate + greeks exposure caps.

These use mocked facts (no live Alpaca calls) and a separate test DB. Run with:
    python -m pytest tests/test_option_risk.py -v
"""

import sys
import os
import unittest
from datetime import date, timedelta

os.environ["DATABASE_FILENAME"] = "test_trading_agent.db"

sys.path.insert(0, r"Z:\python\projects")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _mock_earnings(symbols, next_days):
    """Build a fake data_provider.get_earnings_dates returning earnings dates.

    next_days > 0 schedules an earnings date `next_days` days from today for each
    symbol. Returns a callable that returns a pandas DataFrame, or None if no
    symbols requested (so the event gate fails open with no false flatten).
    """
    import pandas as pd

    def _fetch(tickers=None, days_ahead=365):
        wanted = {str(t).upper() for t in (tickers or [])}
        rows = []
        for sym in symbols:
            if sym.upper() in wanted:
                d = date.today() + timedelta(days=next_days)
                rows.append({"ticker": sym.upper(), "earnings_date": d})
        if not rows:
            return pd.DataFrame(columns=["ticker", "earnings_date"])
        return pd.DataFrame(rows)

    return _fetch


class TestEventGate(unittest.TestCase):
    def setUp(self):
        import core.config as cfg
        self._prev = (getattr(cfg, "OPTIONS_EVENT_GATE_ENABLED", None),
                      getattr(cfg, "OPTIONS_EVENT_GATE_INCLUDE_FOMC", None))
        cfg.OPTIONS_EVENT_GATE_ENABLED = True
        cfg.OPTIONS_EVENT_GATE_INCLUDE_FOMC = True

    def tearDown(self):
        import core.config as cfg
        cfg.OPTIONS_EVENT_GATE_ENABLED, cfg.OPTIONS_EVENT_GATE_INCLUDE_FOMC = self._prev

    def test_no_imminent_earnings_no_close(self):
        from core import option_risk as orm
        import core.data_provider as dp
        orig = getattr(dp, "get_earnings_dates", None)
        dp.get_earnings_dates = _mock_earnings(["NVDA"], 45)  # far out
        try:
            reason = orm.event_gate_close_reason(
                "NVDA261216C00230000", as_of=date.today())
            self.assertIsNone(reason)
        finally:
            dp.get_earnings_dates = orig

    def test_earnings_imminent_flattens(self):
        from core import option_risk as orm
        import core.data_provider as dp
        orig = getattr(dp, "get_earnings_dates", None)
        dp.get_earnings_dates = _mock_earnings(["NVDA"], 1)  # earnings tomorrow
        try:
            reason = orm.event_gate_close_reason(
                "NVDA261216C00230000", as_of=date.today())
            self.assertIsNotNone(reason)
            self.assertIn("earnings", reason.lower())
        finally:
            dp.get_earnings_dates = orig

    def test_fomc_window_flattens(self):
        from core import option_risk as orm
        import core.data_provider as dp
        orig = getattr(dp, "get_earnings_dates", None)
        dp.get_earnings_dates = _mock_earnings([], 0)  # no earnings
        try:
            # FOMC 2026-09-15: as_of 2026-09-14 => T+1 is FOMC day.
            reason = orm.event_gate_close_reason(
                "NVDA261216C00230000", as_of=date(2026, 9, 14))
            self.assertIsNotNone(reason)
            self.assertIn("fomc", reason.lower())
        finally:
            dp.get_earnings_dates = orig

    def test_event_gate_disabled(self):
        import core.config as cfg
        from core import option_risk as orm
        cfg.OPTIONS_EVENT_GATE_ENABLED = False
        import core.data_provider as dp
        orig = getattr(dp, "get_earnings_dates", None)
        dp.get_earnings_dates = _mock_earnings(["NVDA"], 1)
        try:
            reason = orm.event_gate_close_reason(
                "NVDA261216C00230000", as_of=date.today())
            self.assertIsNone(reason)
        finally:
            dp.get_earnings_dates = orig


class _FakeClient:
    """Minimal fake alpaca client for greeks/position lookups."""

    def __init__(self, vega=None, delta=None, qty=1, market_value=0.0, greeks_available=True):
        self._vega = vega
        self._delta = delta
        self._qty = qty
        self._mv = market_value
        self._greeks_available = greeks_available

    def get_latest_option_data(self, symbols):
        if not self._greeks_available:
            return {}
        class _G:
            def __init__(s, v, d):
                s.vega = v
                s.delta = d
        class _Q:
            def __init__(s, g):
                s.greeks = g
        return {s: _Q(_G(self._vega, self._delta)) for s in symbols}

    def get_option_positions(self):
        return {"NVDA261216C00230000": {"qty": self._qty, "market_value": self._mv}}


class TestExposureCaps(unittest.TestCase):
    def setUp(self):
        import core.config as cfg
        self._prev = (cfg.OPTIONS_VEGA_CAP_MV_PCT, cfg.OPTIONS_DELTA_CAP_PCT,
                      cfg.OPTIONS_EOD_FLAT, cfg.OPTIONS_ENABLED)
        cfg.OPTIONS_VEGA_CAP_MV_PCT = 0.02
        cfg.OPTIONS_DELTA_CAP_PCT = 0.15
        cfg.OPTIONS_EOD_FLAT = False
        cfg.OPTIONS_ENABLED = True

    def tearDown(self):
        import core.config as cfg
        (cfg.OPTIONS_VEGA_CAP_MV_PCT, cfg.OPTIONS_DELTA_CAP_PCT,
         cfg.OPTIONS_EOD_FLAT, cfg.OPTIONS_ENABLED) = self._prev

    def test_vega_cap_trips(self):
        from core import option_risk as orm
        # 10 contracts of vega 10 => $100 of vega > 2% of 100k ($2000)? no.
        # Use high vega + 5 contracts to exceed.
        client = _FakeClient(vega=100.0, delta=0.5, qty=5)
        # vega_dol = 100*5 = 500; cap = 100k*0.02 = 2000 -> NOT trip. Bump vega.
        client = _FakeClient(vega=500.0, delta=0.5, qty=5)  # 2500 > 2000
        reason = orm.exposure_cap_reason(client, "NVDA261216C00230000", equity=100000.0)
        self.assertIsNotNone(reason)
        self.assertIn("vega", reason.lower())

    def test_delta_cap_trips(self):
        from core import option_risk as orm
        # delta 3000 * 1 contract = $3000 of directional exposure.
        # cap = 0.15 * equity. Use equity=1e6 => cap $150k, so bump delta higher.
        client = _FakeClient(vega=0.0, delta=300000.0, qty=1)  # 300000 > 150k
        reason = orm.exposure_cap_reason(client, "NVDA261216C00230000", equity=1000000.0)
        self.assertIsNotNone(reason)
        self.assertIn("delta", reason.lower())

    def test_no_greeks_premium_fallback(self):
        from core import option_risk as orm
        client = _FakeClient(greeks_available=False, qty=10, market_value=100000.0)
        # premium 100k > 5% alloc cap? 5% of equity; equity=1e6 => 50k -> trip
        reason = orm.exposure_cap_reason(client, "NVDA261216C00230000", equity=1000000.0)
        # premium fallback uses OPTIONS_MAX_ALLOCATION_PCT (0.05) * equity 1e6 = 50k < 100k
        self.assertIsNotNone(reason)


if __name__ == "__main__":
    unittest.main()