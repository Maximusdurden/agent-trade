"""Regression tests for the swing OPG submission-window fix (TMCL-1062).

Alpaca rejects OPG (market-on-open) orders submitted after 9:28 AM but before
7:00 PM ET with code 40310000. The EOD scan fires at 4:05 PM ET, so staging
there always failed and no swing entry was ever placed. These tests pin the
window logic and the deferred-staging behavior.
"""

import os
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

os.environ["DATABASE_FILENAME"] = "test_swing_opg_window.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_swing_trader as rst

ET = ZoneInfo("America/New_York")


def _et(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=ET)


class TestOpgWindowState(unittest.TestCase):
    def test_rejects_the_405pm_eod_scan_time(self):
        # 2026-10-01 is a Thursday; 4:05 PM ET is the exact failing timestamp.
        is_open, reason = rst._opg_window_state(_et(2026, 10, 1, 16, 5))
        self.assertFalse(is_open)
        self.assertIn("OPG window closed", reason)

    def test_closed_immediately_after_928am(self):
        is_open, _ = rst._opg_window_state(_et(2026, 10, 1, 9, 29))
        self.assertFalse(is_open)

    def test_open_just_before_928am(self):
        is_open, _ = rst._opg_window_state(_et(2026, 10, 1, 9, 27))
        self.assertTrue(is_open)

    def test_open_at_700pm(self):
        is_open, _ = rst._opg_window_state(_et(2026, 10, 1, 19, 0))
        self.assertTrue(is_open)

    def test_closed_just_before_700pm(self):
        is_open, _ = rst._opg_window_state(_et(2026, 10, 1, 18, 59))
        self.assertFalse(is_open)

    def test_saturday_is_closed(self):
        # 2026-10-03 is a Saturday; there is no opening auction to queue for.
        is_open, reason = rst._opg_window_state(_et(2026, 10, 3, 20, 0))
        self.assertFalse(is_open)
        self.assertIn("Saturday", reason)

    def test_sunday_evening_is_open(self):
        # 2026-10-04 is a Sunday; 8:00 PM ET queues for Monday's open.
        is_open, _ = rst._opg_window_state(_et(2026, 10, 4, 20, 0))
        self.assertTrue(is_open)


class TestPlaceMarketOnOpenOrder(unittest.TestCase):
    def _client(self):
        client = MagicMock()
        client.trading_client.submit_order.return_value = MagicMock(id="order-1")
        return client

    def test_uses_opg_inside_window(self):
        from alpaca.trading.enums import TimeInForce

        client = self._client()
        with patch.object(rst, "_opg_window_state", return_value=(True, "open")):
            res = rst.place_market_on_open_order(client, "AAPL", 10, "buy")
        self.assertEqual(res["status"], "submitted")
        req = client.trading_client.submit_order.call_args.kwargs["order_data"]
        self.assertEqual(req.time_in_force, TimeInForce.OPG)

    def test_falls_back_to_day_outside_window(self):
        from alpaca.trading.enums import TimeInForce

        client = self._client()
        with patch.object(rst, "_opg_window_state", return_value=(False, "closed")):
            res = rst.place_market_on_open_order(client, "AAPL", 10, "buy")
        self.assertEqual(res["status"], "submitted")
        req = client.trading_client.submit_order.call_args.kwargs["order_data"]
        self.assertEqual(req.time_in_force, TimeInForce.DAY)


class TestEodScanDefersStaging(unittest.TestCase):
    def test_scan_defers_when_opg_window_closed(self):
        candidates = [{"symbol": "AAPL", "signal_date": "2026-10-01", "rsi": 5.0,
                       "stretch": 1.0, "close": 100.0}]
        client = MagicMock()
        client.get_account_state.return_value = {"equity": 100000.0}

        with patch("core.strategies.swing_rsi2_mean_reversion.scan_signals",
                   return_value=candidates), \
             patch("core.alpaca_client.AlpacaClient", return_value=client), \
             patch.object(rst, "_load_positions", return_value={}), \
             patch.object(rst, "_opg_window_state", return_value=(False, "closed")), \
             patch("core.strategies.swing_rsi2_mean_reversion.stage_orders") as stage_mock, \
             patch.object(rst, "place_market_on_open_order") as place_mock:
            result = rst.run_eod_scan(dry_run=False)

        self.assertTrue(result["deferred"])
        self.assertEqual(result["staged"], 0)
        stage_mock.assert_not_called()
        place_mock.assert_not_called()


class TestAutoModeSelection(unittest.TestCase):
    """The 7:05 PM OPG staging run must select scan mode, not monitor mode."""

    def _run_auto_at(self, when):
        with patch.object(rst, "_now_et", return_value=when), \
             patch.object(rst, "_sync_down_from_gcs"), \
             patch.object(rst, "_sync_up_to_gcs"), \
             patch.object(rst, "setup_jira_logging"), \
             patch("core.gcs_sync.download_from_gcs"), \
             patch("core.gcs_sync.upload_to_gcs"), \
             patch.object(rst, "run_eod_scan", return_value={}) as scan_mock, \
             patch.object(rst, "run_monitor", return_value={}) as monitor_mock, \
             patch.object(sys, "argv", ["run_swing_trader.py", "--auto"]):
            rst.main()
        return scan_mock, monitor_mock

    def test_405pm_selects_scan(self):
        scan_mock, monitor_mock = self._run_auto_at(_et(2026, 10, 1, 16, 5))
        scan_mock.assert_called_once()
        monitor_mock.assert_not_called()

    def test_705pm_selects_scan_for_opg_staging(self):
        scan_mock, monitor_mock = self._run_auto_at(_et(2026, 10, 1, 19, 5))
        scan_mock.assert_called_once()
        monitor_mock.assert_not_called()

    def test_midday_selects_monitor(self):
        scan_mock, monitor_mock = self._run_auto_at(_et(2026, 10, 1, 11, 30))
        monitor_mock.assert_called_once()
        scan_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
