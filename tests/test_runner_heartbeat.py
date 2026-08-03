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


if __name__ == "__main__":
    unittest.main()