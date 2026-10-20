"""Tests for spread-aware strike selection in options_strike_sizer.

Verifies (offline, no Alpaca calls):
  - select_strike prefers the first OTM strike that passes the spread gate.
  - When the closest OTM fails the spread gate, it walks further OTM to a
    strike that passes.
  - When NO strike passes, it falls back to the closest OTM (the runner's
    spread gate rejects it there) and reports spread info.
  - _delta_target_fallback prefers spread-passing strikes.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sideload import options_strike_sizer as ss


class _Quote:
    def __init__(self, bid, ask):
        self.bid_price = bid
        self.ask_price = ask


class _Snapshot:
    def __init__(self, bid, ask, delta=None):
        self.latest_quote = _Quote(bid, ask)
        if delta is not None:
            self.greeks = mock.MagicMock(delta=delta)
        else:
            self.greeks = None


def _chain(*contracts):
    """Build a chain dict from (occ, bid, ask, delta) tuples."""
    return {occ: _Snapshot(bid, ask, delta) for occ, bid, ask, delta in contracts}


class TestSelectStrikeSpreadAware(unittest.TestCase):
    def _run(self, chain, current_price=100.0, direction="BULLISH"):
        client = mock.MagicMock()
        client.get_latest_price.return_value = current_price
        client.get_option_chain_snapshot.return_value = chain
        return ss.select_strike(client, "TEST", direction, "2026-10-16",
                                current_price=current_price)

    def test_prefers_first_otm_that_passes_spread(self):
        """Closest OTM fails spread; next OTM passes -> select the passing one."""
        # TEST 100C: bid 0.90 ask 1.00 -> spread 0.10/1.00 = 10% (FAIL, ask<3 -> abs cap 0.04)
        # TEST 101C: bid 0.97 ask 1.00 -> spread 0.03 (PASS abs cap)
        chain = _chain(
            ("TEST261016C00100000", 0.90, 1.00, 0.50),  # dist 0, FAIL
            ("TEST261016C00101000", 0.97, 1.00, 0.45),  # dist 1, PASS
        )
        result = self._run(chain)
        self.assertEqual(result["selected_occ"], "TEST261016C00101000")
        self.assertTrue(result["spread_pass"])
        self.assertEqual(result["spread"], 0.03)

    def test_selects_first_otm_when_spread_ok(self):
        """Closest OTM passes spread -> unchanged behavior."""
        chain = _chain(
            ("TEST261016C00100000", 0.97, 1.00, 0.50),  # dist 0, PASS
            ("TEST261016C00101000", 0.90, 1.00, 0.45),  # dist 1, PASS
        )
        result = self._run(chain)
        self.assertEqual(result["selected_occ"], "TEST261016C00100000")
        self.assertTrue(result["spread_pass"])

    def test_falls_back_to_closest_otm_when_all_fail(self):
        """No strike passes -> closest OTM selected, spread_pass False."""
        chain = _chain(
            ("TEST261016C00100000", 0.90, 1.00, 0.50),  # dist 0, FAIL
            ("TEST261016C00101000", 0.90, 1.00, 0.45),  # dist 1, FAIL
        )
        result = self._run(chain)
        self.assertEqual(result["selected_occ"], "TEST261016C00100000")
        self.assertFalse(result["spread_pass"])
        self.assertIsNotNone(result["spread_rule"])

    def test_expensive_contract_fallback_prefers_spread_pass(self):
        """Delta-target fallback prefers a spread-passing strike."""
        # First-OTM ask too expensive (> ELASTIC_MAX_NOTIONAL=650 -> ask > 6.50).
        # Both fallback candidates are delta 0.45; one fails spread, one passes.
        chain = _chain(
            ("TEST261016C00100000", 6.90, 7.00, 0.50),   # dist 0, too expensive
            ("TEST261016C00102000", 5.90, 6.00, 0.45),   # dist 2, FAIL (0.10/6.00=1.7%? no: 0.10/6.00 = 1.67% PASS)
            ("TEST261016C00103000", 5.90, 6.00, 0.45),   # dist 3, PASS
        )
        # Force the first-OTM to be too expensive and the fallback to engage.
        result = self._run(chain)
        # 100C ask 7.00 -> notional 700 > 650 -> fallback to delta 0.45.
        self.assertEqual(result["selected_occ"], "TEST261016C00102000")
        self.assertTrue(result["spread_pass"])

    def test_put_side_spread_aware(self):
        """Same logic applies for puts (dist = current - strike)."""
        chain = _chain(
            ("TEST261016P00100000", 0.90, 1.00, -0.50),  # dist 0, FAIL
            ("TEST261016P00099000", 0.97, 1.00, -0.45),  # dist 1, PASS
        )
        result = self._run(chain, direction="BEARISH")
        self.assertEqual(result["selected_occ"], "TEST261016P00099000")
        self.assertTrue(result["spread_pass"])

    def test_delta_floor_skips_deep_otm(self):
        """A spread-passing strike below DELTA_FLOOR is skipped (leverage decay)."""
        # 100C delta 0.50 FAIL spread; 101C delta 0.28 PASS spread but below floor;
        # 102C delta 0.35 PASS spread and above floor -> selected.
        chain = _chain(
            ("TEST261016C00100000", 0.90, 1.00, 0.50),   # dist 0, FAIL
            ("TEST261016C00101000", 0.97, 1.00, 0.28),   # dist 1, PASS but < floor
            ("TEST261016C00102000", 0.97, 1.00, 0.35),   # dist 2, PASS >= floor
        )
        result = self._run(chain)
        self.assertEqual(result["selected_occ"], "TEST261016C00102000")
        self.assertTrue(result["spread_pass"])

    def test_delta_floor_put_side_abs(self):
        """Delta floor uses abs() so negative put deltas are handled."""
        chain = _chain(
            ("TEST261016P00100000", 0.90, 1.00, -0.50),  # dist 0, FAIL
            ("TEST261016P00099000", 0.97, 1.00, -0.28),  # dist 1, PASS but < floor
            ("TEST261016P00098000", 0.97, 1.00, -0.35),  # dist 2, PASS >= floor
        )
        result = self._run(chain, direction="BEARISH")
        self.assertEqual(result["selected_occ"], "TEST261016P00098000")
        self.assertTrue(result["spread_pass"])

    def test_delta_none_degrades_gracefully(self):
        """When greeks are unavailable (paper), delta None passes the floor check."""
        chain = _chain(
            ("TEST261016C00100000", 0.90, 1.00, 0.50),   # dist 0, FAIL
            ("TEST261016C00101000", 0.97, 1.00, None),   # dist 1, PASS, no delta
        )
        result = self._run(chain)
        self.assertEqual(result["selected_occ"], "TEST261016C00101000")
        self.assertTrue(result["spread_pass"])


class TestDeltaTargetFallbackSpreadAware(unittest.TestCase):
    def test_prefers_spread_pass(self):
        candidates = [
            {"occ": "A", "delta": 0.45, "ask": 5.0, "dist": 2.0, "spread_pass": False},
            {"occ": "B", "delta": 0.45, "ask": 5.0, "dist": 3.0, "spread_pass": True},
            {"occ": "C", "delta": 0.45, "ask": 5.0, "dist": 4.0, "spread_pass": True},
        ]
        result = ss._delta_target_fallback(candidates, "call", 650.0)
        # B and C both pass spread with identical delta/ask; B is closer OTM.
        self.assertEqual(result["occ"], "B")

    def test_spread_fail_loses_to_further_otm_pass(self):
        """A spread-failing closer strike loses to a spread-passing further one."""
        candidates = [
            {"occ": "A", "delta": 0.45, "ask": 5.0, "dist": 2.0, "spread_pass": False},
            {"occ": "B", "delta": 0.45, "ask": 5.0, "dist": 5.0, "spread_pass": True},
        ]
        result = ss._delta_target_fallback(candidates, "call", 650.0)
        self.assertEqual(result["occ"], "B")

    def test_returns_none_when_no_fit(self):
        candidates = [
            {"occ": "A", "delta": 0.60, "ask": 5.0, "dist": 2.0, "spread_pass": True},
        ]
        result = ss._delta_target_fallback(candidates, "call", 650.0)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()