import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import runner


class TestRunnerHeartbeat(unittest.TestCase):
    @patch("core.gcs_sync.upload_to_gcs")
    @patch("runner._run_trading_cycle_impl")
    @patch("runner.database.record_cycle_heartbeat")
    def test_successful_cycle_records_started_and_completed(
        self, record_heartbeat, cycle_impl, upload
    ):
        cycle_impl.return_value = ("COMPLETED", "CRYPTO_ONLY", "Cycle completed with HOLD decision.")

        result = runner.run_trading_cycle(
            MagicMock(), MagicMock(), MagicMock(), MagicMock(), dry_run=True
        )

        self.assertEqual(result[0], "COMPLETED")
        self.assertEqual(record_heartbeat.call_count, 2)
        self.assertEqual(record_heartbeat.call_args_list[0].args[0], "STARTED")
        self.assertEqual(record_heartbeat.call_args_list[1].args[0], "COMPLETED")
        self.assertEqual(record_heartbeat.call_args_list[1].args[1], "CRYPTO_ONLY")
        upload.assert_called_once()

    @patch("core.gcs_sync.upload_to_gcs")
    @patch("runner._run_trading_cycle_impl", side_effect=RuntimeError("boom"))
    @patch("runner.database.record_cycle_heartbeat")
    def test_unhandled_failure_records_failed_terminal_state(
        self, record_heartbeat, cycle_impl, upload
    ):
        with self.assertRaises(RuntimeError):
            runner.run_trading_cycle(MagicMock(), MagicMock(), MagicMock(), MagicMock())

        self.assertEqual(record_heartbeat.call_args_list[-1].args[0], "FAILED")
        self.assertIn("RuntimeError: boom", record_heartbeat.call_args_list[-1].args[2])
        upload.assert_called_once()


class TestIntradayOptionsWatch(unittest.TestCase):
    @patch("core.database.set_system_state")
    @patch("core.database.get_system_state", return_value=None)
    @patch("core.strategist.MetaStrategist")
    def test_no_tune_when_no_options_held(self, strategist_cls, get_state, set_state):
        positions = {"NVDA": {"qty": 30}}
        ran = runner.maybe_run_intraday_options_watch(MagicMock(), positions)
        self.assertFalse(ran)
        strategist_cls.assert_not_called()

    @patch("core.database.set_system_state")
    @patch("core.database.get_system_state", return_value=None)
    @patch("core.strategist.MetaStrategist")
    def test_tunes_when_option_held_and_no_prior_watch(self, strategist_cls, get_state, set_state):
        positions = {"NVDA": {"qty": 30}, "NVDA261016C00230000": {"qty": 4}}
        strategist_cls.return_value.run_option_strategy_refinement.return_value = ["NVDA"]
        ran = runner.maybe_run_intraday_options_watch(MagicMock(), positions)
        self.assertTrue(ran)
        strategist_cls.return_value.run_option_strategy_refinement.assert_called_once()
        set_state.assert_called_once()
        # Stored state key + a value
        self.assertEqual(set_state.call_args.args[0], "last_options_intraday_watch")

    @patch("core.database.set_system_state")
    @patch("core.database.get_system_state")
    @patch("core.strategist.MetaStrategist")
    def test_skips_when_within_cooldown(self, strategist_cls, get_state, set_state):
        # A recent timestamp (within 30m) should suppress the tune.
        from datetime import datetime, timedelta, timezone
        recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        get_state.return_value = recent
        positions = {"NVDA261016C00230000": {"qty": 4}}
        ran = runner.maybe_run_intraday_options_watch(MagicMock(), positions)
        self.assertFalse(ran)
        strategist_cls.return_value.run_option_strategy_refinement.assert_not_called()


if __name__ == "__main__":
    unittest.main()