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

    Tests the LIVE runner logic (_model_a_setup_for) directly, mocking only
    the intraday fetch and clock. A sub-cent wiggle (e.g. Oct 6's $0.03 dip
    below PML) must NOT trigger; a genuine sweep (>= SWEEP_MIN_PENETRATION)
    must.
    """

    @mock.patch("sideload.runner_options_multiticker.datetime")
    @mock.patch("sideload.runner_options_multiticker._load_intraday")
    def test_shallow_penetration_does_not_trigger(self, mock_load, mock_dt):
        """Oct 6 case: $0.03 below PML must NOT fire on the live runner."""
        import pandas as pd
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        ts = pd.Timestamp("2026-10-06 09:40:00", tz=ET)

        # Completed candle mock: bullish sweep with only $0.03 penetration.
        bars = pd.DataFrame(
            [{"high": 381.10, "low": 379.97, "close": 381.10, "volume": 1000}],
            index=pd.DatetimeIndex([ts]),
        )
        mock_load.return_value = bars

        # Frozen mock time 90s after bar close: the 09:40 bar is a completed
        # candle (cutoff 09:41:00) and passes the 120s freshness check, so the
        # penetration threshold is genuinely exercised.
        mock_dt.now.return_value = ts + pd.Timedelta(seconds=90)

        result = mt._model_a_setup_for(
            client=mock.MagicMock(),
            symbol="TSLA",
            session_date="2026-10-06",
            pmh=383.66,
            pml=380.00,
        )
        self.assertIsNone(result, "A $0.03 penetration must not trigger a setup in the live runner")

    @mock.patch("sideload.runner_options_multiticker.datetime")
    @mock.patch("sideload.runner_options_multiticker._load_intraday")
    def test_genuine_sweep_triggers(self, mock_load, mock_dt):
        """Oct 7 case: >$1 penetration must fire on the live runner."""
        import pandas as pd
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        ts = pd.Timestamp("2026-10-07 09:35:00", tz=ET)

        # Prepend a 09:30 open bar so VWAP evaluates above the sweep close.
        ts_open = pd.Timestamp("2026-10-07 09:30:00", tz=ET)
        bars = pd.DataFrame(
            [
                {"high": 381.00, "low": 379.00, "close": 380.00, "volume": 5000},
                {"high": 380.90, "low": 377.00, "close": 378.89, "volume": 1500},
            ],
            index=pd.DatetimeIndex([ts_open, ts]),
        )
        mock_load.return_value = bars

        # Frozen mock time 90s after bar close: the 09:35 bar is a completed
        # candle (cutoff 09:36:00) and passes the 120s freshness check, so the
        # sweep is genuinely evaluated.
        mock_dt.now.return_value = ts + pd.Timedelta(seconds=90)

        result = mt._model_a_setup_for(
            client=mock.MagicMock(),
            symbol="TSLA",
            session_date="2026-10-07",
            pmh=379.47,
            pml=376.80,
        )
        self.assertIsNotNone(result, "A $1.43 penetration must trigger a setup in the live runner")
        self.assertEqual(result["direction"], "BEARISH")


class TestSweepMaxPMRangeCeiling(unittest.TestCase):
    """Model A setup respects the OPTIONS_SWEEP_MAX_PM_RANGE ceiling.

    When the env var is set (> 0), a setup on a day whose pre-market range
    (PMH - PML) exceeds the ceiling must NOT trigger. When disabled (0, the
    default), the same setup must trigger. Mirrors the penetration tests.
    """

    def _make_bars(self, ts):
        import pandas as pd
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        ts_open = ts - pd.Timedelta(minutes=5)
        return pd.DataFrame(
            [
                {"high": 381.00, "low": 379.00, "close": 380.00, "volume": 5000},
                {"high": 380.90, "low": 377.00, "close": 378.89, "volume": 1500},
            ],
            index=pd.DatetimeIndex([ts_open, ts]),
        )

    @mock.patch("sideload.runner_options_multiticker.datetime")
    @mock.patch("sideload.runner_options_multiticker._load_intraday")
    def test_wide_pm_range_blocked_when_ceiling_enabled(self, mock_load, mock_dt):
        """PM range $4.50 > $4.00 ceiling must NOT trigger."""
        import pandas as pd
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        ts = pd.Timestamp("2026-10-08 09:35:00", tz=ET)
        mock_load.return_value = self._make_bars(ts)
        mock_dt.now.return_value = ts + pd.Timedelta(seconds=90)

        with mock.patch.dict(os.environ, {"OPTIONS_SWEEP_MAX_PM_RANGE": "4.0"}):
            result = mt._model_a_setup_for(
                client=mock.MagicMock(),
                symbol="TSLA",
                session_date="2026-10-08",
                pmh=379.47,   # PM range = 379.47 - 374.97 = $4.50 > $4.00
                pml=374.97,
            )
            self.assertIsNone(result, "PM range $4.50 > $4.00 ceiling must not trigger")

    @mock.patch("sideload.runner_options_multiticker.datetime")
    @mock.patch("sideload.runner_options_multiticker._load_intraday")
    def test_wide_pm_range_allowed_when_ceiling_disabled(self, mock_load, mock_dt):
        """PM range $4.50 with ceiling disabled (default 0) must trigger."""
        import pandas as pd
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        ts = pd.Timestamp("2026-10-08 09:35:00", tz=ET)
        mock_load.return_value = self._make_bars(ts)
        mock_dt.now.return_value = ts + pd.Timedelta(seconds=90)

        with mock.patch.dict(os.environ, {"OPTIONS_SWEEP_MAX_PM_RANGE": "0"}):
            result = mt._model_a_setup_for(
                client=mock.MagicMock(),
                symbol="TSLA",
                session_date="2026-10-08",
                pmh=379.47,
                pml=374.97,
            )
            self.assertIsNotNone(result, "PM range $4.50 must trigger when ceiling is disabled")
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


class TestPhantomPositionRecovery(unittest.TestCase):
    """Regression: a tracked position the broker no longer holds (phantom) must
    be treated as closed so state is cleared, not crash the runner.

    Root cause (2026-10-09/12): state was written without a confirmed fill, then
    every run re-attached to _monitor_position -> _dispatch_exit ->
    close_option_position, which raised APIError 40410000 ("position not found")
    and crashed instead of clearing the phantom.
    """

    def test_dispatch_exit_treats_position_not_found_as_closed(self):
        from sideload import runner_options_tsla as tsla

        class FakeClient:
            def close_option_position(self, symbol):
                raise Exception('{"code":40410000,"message":"position not found: TSLA261016P00380000"}')

        result = tsla._dispatch_exit(
            FakeClient(), "TSLA261016P00380000", 1, "time_stop", dry_run=False)
        self.assertEqual(result["status"], "closed")
        self.assertTrue(result.get("already_closed"))
        self.assertIsNone(result.get("filled_avg_price"))

    def test_finalize_exit_clears_state_when_already_closed(self):
        from sideload import runner_options_tsla as tsla

        pos = {
            "contract_symbol": "TSLA261016P00380000",
            "entry_time": "2026-10-09T09:32:00-04:00",
            "entry_premium": 6.34,
            "contracts": 1,
            "target_premium": 9.19,
            "stop_premium": 4.95,
            "session_date": "2026-10-09",
        }
        result = {"status": "closed", "reason": "time_stop",
                  "filled_avg_price": None, "already_closed": True}
        exit_info = tsla._finalize_exit(pos, result, "time_stop", dry_run=False)
        self.assertTrue(exit_info["closed"], "already_closed must clear state")

    def test_dispatch_exit_reraises_other_errors(self):
        from sideload import runner_options_tsla as tsla

        class FakeClient:
            def close_option_position(self, symbol):
                raise RuntimeError("network timeout")

        with self.assertRaises(RuntimeError):
            tsla._dispatch_exit(FakeClient(), "TSLA261016P00380000", 1,
                                "time_stop", dry_run=False)


class TestEntryFillConfirmation(unittest.TestCase):
    """Regression: an entry order without a confirmed fill price must NOT write
    state (phantom-position prevention).

    Root cause: _enter_position accepted 'partially_filled' status and defaulted
    fill_price to ask when None, writing an active_position the broker never
    held.
    """

    def _make_client(self, order_status="filled", fill_price=None):
        class FakeTradingClient:
            def __init__(self):
                self.canceled = []

            def get_order_by_id(self, order_id):
                class Order:
                    status = order_status
                    filled_avg_price = fill_price
                    filled_qty = 1
                return Order()

            def cancel_order_by_id(self, order_id):
                self.canceled.append(order_id)

        class FakeClient:
            def __init__(self):
                self.trading_client = FakeTradingClient()

            def place_option_order(self, **kwargs):
                return {"id": "ord-1", "status": "new", "filled_avg_price": None}

        return FakeClient()

    def test_unfilled_entry_does_not_write_state(self):
        from sideload import runner_options_multiticker as mt

        client = self._make_client(order_status="new", fill_price=None)
        # Mock the strike selection + spread gate to return a valid contract.
        with mock.patch.object(mt, "resolve_front_week_expiry", return_value="2026-10-16"), \
             mock.patch.object(mt, "select_strike", return_value={
                 "selected_occ": "TSLA261016P00380000", "contracts": 1,
                 "ask": 6.34, "bid": 6.20, "strike": 380.0, "delta": 0.45,
             }), \
             mock.patch.object(mt, "check_spread", return_value={"pass": True}):
            pos = mt._enter_position(
                client, "TSLA", "2026-10-12",
                {"direction": "BEARISH", "entry_price": 381.91},
                dry_run=False,
            )
        self.assertIsNone(pos, "Unfilled entry must not produce a tracked position")
        self.assertEqual(client.trading_client.canceled, ["ord-1"], "Unfilled order must be canceled")


if __name__ == "__main__":
    unittest.main()