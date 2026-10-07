"""Tests for the Phase 4 scaled guardrails in runner_options_multiticker.

Verifies (offline, no Alpaca calls):
  - _correlation_conflict blocks QQQ+SPY / NVDA+AMD pairs.
  - _rank_armed_by_pm_range keeps only the top-N by PM range.
  - _batch_load_intraday falls back to per-symbol fetch when the batch returns
    daily bars (non-intraday) or an empty frame.
  - check_can_trade honors max_trades.
  - check_concurrent_position_cap blocks at the ceiling.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sideload import runner_options_multiticker as mt
from sideload import options_execution_guards as guards


class TestCorrelationMutex(unittest.TestCase):
    def test_blocks_qqq_when_spy_active(self):
        conflict = mt._correlation_conflict("QQQ", {"SPY"})
        self.assertEqual(conflict, "SPY")

    def test_blocks_nvda_when_amd_active(self):
        conflict = mt._correlation_conflict("NVDA", {"AMD"})
        self.assertEqual(conflict, "AMD")

    def test_allows_unrelated(self):
        self.assertIsNone(mt._correlation_conflict("TSLA", {"SPY"}))
        self.assertIsNone(mt._correlation_conflict("META", {"NVDA"}))


class TestPriorityAllocation(unittest.TestCase):
    def test_keeps_top_n_by_range(self):
        armed = {
            "A": {"range_pct": 0.5},
            "B": {"range_pct": 1.2},
            "C": {"range_pct": 0.8},
            "D": {"range_pct": 2.0},
        }
        top = mt._rank_armed_by_pm_range(armed, 2)
        self.assertEqual(list(top.keys()), ["D", "B"])

    def test_keeps_all_when_under_cap(self):
        armed = {"A": {"range_pct": 0.5}, "B": {"range_pct": 0.6}}
        top = mt._rank_armed_by_pm_range(armed, 3)
        self.assertEqual(set(top.keys()), {"A", "B"})


class TestBatchLoadFallback(unittest.TestCase):
    def test_falls_back_when_batch_returns_daily_bars(self):
        # Fake client whose get_historical_bars returns daily bars (1 row) and
        # whose paginated path returns real intraday.
        class FakeClient:
            def get_historical_bars(self, symbols, limit=100, timeframe_str="day",
                                    max_retries=3):
                import pandas as pd
                idx = pd.DatetimeIndex([pd.Timestamp("2026-09-24", tz="UTC")])
                return pd.DataFrame({"high": [1.0], "low": [0.9], "close": [0.95],
                                     "volume": [100]}, index=idx)

            def get_historical_bars_paginated(self, symbol, timeframe_str="1min",
                                              days_back=5):
                import pandas as pd
                from datetime import timedelta
                rows = []
                for minute in range(0, 10):
                    ts = pd.Timestamp("2026-09-25 04:00:00", tz="America/New_York") \
                        + timedelta(minutes=minute)
                    rows.append({"high": 255.0, "low": 254.0, "close": 254.5,
                                 "volume": 500})
                idx = pd.DatetimeIndex(
                    [pd.Timestamp("2026-09-25 04:00:00", tz="America/New_York")
                     + timedelta(minutes=i) for i in range(10)])
                return pd.DataFrame(rows, index=idx)

        client = FakeClient()
        result = mt._batch_load_intraday(client, ["TSLA"], days_back=5)
        self.assertIn("TSLA", result)
        # The fallback path returns the paginated intraday frame (10 rows).
        self.assertEqual(len(result["TSLA"]), 10)

    def test_falls_back_when_batch_returns_empty(self):
        class FakeClient:
            def get_historical_bars(self, symbols, limit=100, timeframe_str="day",
                                    max_retries=3):
                import pandas as pd
                return pd.DataFrame()

            def get_historical_bars_paginated(self, symbol, timeframe_str="1min",
                                              days_back=5):
                import pandas as pd
                from datetime import timedelta
                rows = [{"high": 1.0, "low": 0.9, "close": 0.95, "volume": 100}]
                idx = pd.DatetimeIndex(
                    [pd.Timestamp("2026-09-25 04:00:00", tz="America/New_York")
                     + timedelta(minutes=i) for i in range(1)])
                return pd.DataFrame(rows, index=idx)

        client = FakeClient()
        result = mt._batch_load_intraday(client, ["TSLA"], days_back=5)
        self.assertIn("TSLA", result)
        self.assertEqual(len(result["TSLA"]), 1)


class TestCircuitBreakerMaxTrades(unittest.TestCase):
    def setUp(self):
        # Isolate the state file.
        self._orig = guards.STATE_FILE
        guards.STATE_FILE = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "scratch_cb_state.json")
        if os.path.exists(guards.STATE_FILE):
            os.remove(guards.STATE_FILE)

    def tearDown(self):
        guards.STATE_FILE = self._orig
        if os.path.exists(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "scratch_cb_state.json")):
            os.remove(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "scratch_cb_state.json"))

    def test_max_trades_3_allows_3(self):
        guards.record_trade("2026-10-06")
        guards.record_trade("2026-10-06")
        guards.record_trade("2026-10-06")
        res = guards.check_can_trade("2026-10-06", max_trades=3)
        self.assertFalse(res["can_trade"])
        self.assertEqual(res["trades_today"], 3)

    def test_max_trades_3_blocks_4th(self):
        guards.record_trade("2026-10-06")
        guards.record_trade("2026-10-06")
        res = guards.check_can_trade("2026-10-06", max_trades=3)
        self.assertTrue(res["can_trade"])
        guards.record_trade("2026-10-06")
        res = guards.check_can_trade("2026-10-06", max_trades=3)
        self.assertFalse(res["can_trade"])


class TestConcurrentPositionCap(unittest.TestCase):
    def test_blocks_at_ceiling(self):
        class FakeClient:
            def get_option_positions(self):
                return {"OCC1": {}, "OCC2": {}, "OCC3": {}}

        res = guards.check_concurrent_position_cap(FakeClient(), max_positions=3)
        self.assertFalse(res["can_enter"])
        self.assertEqual(res["open_count"], 3)

    def test_allows_below_ceiling(self):
        class FakeClient:
            def get_option_positions(self):
                return {"OCC1": {}}

        res = guards.check_concurrent_position_cap(FakeClient(), max_positions=3)
        self.assertTrue(res["can_enter"])
        self.assertEqual(res["open_count"], 1)


if __name__ == "__main__":
    unittest.main()