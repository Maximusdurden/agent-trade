import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.dashboard import get_data_freshness


class TestDashboardFreshness(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc)

    def heartbeat(self, minutes_old=1, status="COMPLETED", scope="CRYPTO_ONLY"):
        timestamp = (self.now - timedelta(minutes=minutes_old)).isoformat().replace("+00:00", "Z")
        return {
            "started_at": timestamp,
            "completed_at": timestamp,
            "status": status,
            "asset_scope": scope,
        }

    def test_fresh_crypto_cycle_is_crypto_only_monitoring(self):
        result = get_data_freshness(self.heartbeat(), 15, self.now)
        self.assertEqual(result["state"], "CRYPTO_ONLY_MONITORING")

    def test_old_heartbeat_is_stale(self):
        result = get_data_freshness(self.heartbeat(minutes_old=36), 15, self.now)
        self.assertEqual(result["state"], "STALE")

    def test_failure_is_reported(self):
        result = get_data_freshness(self.heartbeat(status="NO_MARKET_DATA"), 15, self.now)
        self.assertEqual(result["state"], "FAILED")

    def test_started_cycle_is_running(self):
        heartbeat = self.heartbeat(status="STARTED")
        heartbeat["completed_at"] = ""
        result = get_data_freshness(heartbeat, 15, self.now)
        self.assertEqual(result["state"], "RUNNING")


if __name__ == "__main__":
    unittest.main()