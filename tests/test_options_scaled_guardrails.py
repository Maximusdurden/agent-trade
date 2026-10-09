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


class TestSweepPenetrationThreshold(unittest.TestCase):
    """Model A setup requires a minimum sweep penetration beyond PMH/PML.

    A sub-cent wiggle (e.g. Oct 6's $0.03 dip below PML) must NOT trigger;
    a genuine sweep (>= SWEEP_MIN_PENETRATION) must.
    """

    def _make_bars(self, high, low, close, pmh, pml, ts_str="09:31"):
        """Build a 1-bar frame with the given sweep + a VWAP that confirms."""
        import pandas as pd
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        ts = pd.Timestamp(f"2026-10-06 {ts_str}:00", tz=ET)
        # VWAP: use a value between close and the anchor so the cross confirms.
        # For bearish: close < vwap. For bullish: close > vwap.
        return pd.DataFrame(
            [{"high": high, "low": low, "close": close, "volume": 1000}],
            index=pd.DatetimeIndex([ts]),
        )

    def test_shallow_penetration_does_not_trigger(self):
        """Oct 6 case: $0.03 below PML must NOT fire (below $0.25 threshold)."""
        import pandas as pd
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        pmh, pml = 383.66, 380.00
        # Bullish sweep with only $0.03 penetration below PML.
        ts = pd.Timestamp("2026-10-06 09:40:00", tz=ET)
        bars = pd.DataFrame(
            [{"high": 381.10, "low": 379.97, "close": 381.10, "volume": 1000}],
            index=pd.DatetimeIndex([ts]),
        )
        # VWAP just below close (381.04) so the cross would confirm if penetration passed.
        vwap = pd.Series([381.04], index=pd.DatetimeIndex([ts]))
        setup = mt._model_a_setup_for.__wrapped__ if hasattr(mt._model_a_setup_for, "__wrapped__") else None
        # Directly test the condition via the backtest's model_a_sweep_fade logic:
        from sideload.backtest_entry_ablation import model_a_sweep_fade
        day = pd.Timestamp("2026-10-06", tz=ET)
        anchors = {"pmh": pmh, "pml": pml}
        result = model_a_sweep_fade(bars, day, anchors, vwap)
        self.assertIsNone(result, "A $0.03 penetration must not trigger a setup")

    def test_genuine_sweep_triggers(self):
        """Oct 7 case: >$1 penetration must fire."""
        import pandas as pd
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        pmh, pml = 379.47, 376.80
        # Bearish sweep: high 380.90 > PMH by $1.43, close < PMH, close < VWAP.
        ts = pd.Timestamp("2026-10-07 09:35:00", tz=ET)
        bars = pd.DataFrame(
            [{"high": 380.90, "low": 377.00, "close": 378.89, "volume": 1500}],
            index=pd.DatetimeIndex([ts]),
        )
        vwap = pd.Series([379.50], index=pd.DatetimeIndex([ts]))
        from sideload.backtest_entry_ablation import model_a_sweep_fade
        day = pd.Timestamp("2026-10-07", tz=ET)
        anchors = {"pmh": pmh, "pml": pml}
        result = model_a_sweep_fade(bars, day, anchors, vwap)
        self.assertIsNotNone(result, "A $1.43 penetration must trigger a setup")
        self.assertEqual(result["direction"], "BEARISH")


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