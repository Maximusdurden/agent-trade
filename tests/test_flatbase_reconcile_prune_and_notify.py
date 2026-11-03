# filename: tests/test_flatbase_reconcile_prune_and_notify.py
"""Regression tests for two flat-base live-lane defects found 2026-11-02.

1. reconcile_with_broker() only ever ADDED positions. It adopted broker
   positions missing from state, but never removed state entries the broker no
   longer held. The state therefore only grew, and the reported open-position
   count drifted above reality. Observed: state said 6 open, the broker held 5
   (AMD had been closed but was never pruned), and every hourly cycle
   notification reported the phantom.

2. The monitor scheduler runs hourly 10:00-15:00 ET, and main() sent an
   identical "[FlatBase] Runner cycle complete (N open positions)" message on
   every single run — 6 identical Discord messages per trading day. Now it
   notifies only when the position SET changes, or on the first cycle of the
   day as a liveness heartbeat.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload import flatbase_state as fb_state


def _pos(qty=10.0, entry=100.0):
    """Minimal broker-position stand-in (dict-shaped, as get_positions returns)."""
    return {
        "qty": qty,
        "avg_entry_price": entry,
        "is_option": False,
    }


class _StateFileMixin:
    """Point STATE_PATH at a temp file so tests never touch real state."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_path = fb_state.STATE_PATH
        fb_state.STATE_PATH = os.path.join(self._tmpdir, "flatbase_positions.json")

    def tearDown(self):
        fb_state.STATE_PATH = self._orig_path
        try:
            for name in os.listdir(self._tmpdir):
                os.remove(os.path.join(self._tmpdir, name))
            os.rmdir(self._tmpdir)
        except OSError:
            pass

    def _write(self, symbols):
        state = {
            "active_positions": {
                s: {
                    "cluster_id": f"FB-{s}-20260101-001",
                    "entry_date": "2026-01-01",
                    "bars_held": 1,
                    "entry_price": 100.0,
                    "initial_shares": 10.0,
                    "remaining_shares": 10.0,
                    "stop_loss": 90.0,
                    "target_3r": 130.0,
                    "target_5r": 150.0,
                    "breakeven_active": False,
                    "scale_1_filled": False,
                    "scale_2_filled": False,
                }
                for s in symbols
            },
            "realized_pnl_30d": 0.0,
        }
        fb_state.save_state(state)


class TestReconcilePrunesStalePositions(_StateFileMixin, unittest.TestCase):
    def test_prunes_position_absent_from_broker(self):
        """State has AMD; broker does not -> AMD must be removed."""
        self._write(["MSFT", "AMD"])
        client = MagicMock()
        client.get_positions.return_value = {"MSFT": _pos()}

        with patch.object(fb_state, "load_daily", return_value=None):
            fb_state.reconcile_with_broker(client)

        remaining = set(fb_state.get_open_positions())
        self.assertEqual(remaining, {"MSFT"})
        self.assertNotIn("AMD", remaining)

    def test_prune_reports_actual_count(self):
        """The count used by the notification must match the broker."""
        self._write(["MSFT", "ANET", "NVDA", "SMH", "AMD", "META"])
        client = MagicMock()
        client.get_positions.return_value = {
            s: _pos() for s in ("MSFT", "ANET", "NVDA", "SMH", "META")
        }

        with patch.object(fb_state, "load_daily", return_value=None):
            fb_state.reconcile_with_broker(client)

        self.assertEqual(fb_state.count_open_positions(), 5)

    def test_keeps_positions_still_held(self):
        """Pruning must not touch positions the broker still holds."""
        self._write(["MSFT", "ANET"])
        client = MagicMock()
        client.get_positions.return_value = {"MSFT": _pos(), "ANET": _pos()}

        with patch.object(fb_state, "load_daily", return_value=None):
            fb_state.reconcile_with_broker(client)

        self.assertEqual(set(fb_state.get_open_positions()), {"MSFT", "ANET"})

    def test_broker_failure_does_not_wipe_state(self):
        """A failed broker call must never prune live state."""
        self._write(["MSFT", "AMD"])
        client = MagicMock()
        client.get_positions.side_effect = RuntimeError("API down")

        fb_state.reconcile_with_broker(client)

        self.assertEqual(set(fb_state.get_open_positions()), {"MSFT", "AMD"})

    def test_ignores_options_and_crypto_when_pruning(self):
        """Option/crypto symbols are not equity state and must not drive pruning."""
        self._write(["MSFT"])
        client = MagicMock()
        client.get_positions.return_value = {
            "MSFT": _pos(),
            "TSLA261030C00397500": {"qty": 6, "avg_entry_price": 0.9, "is_option": True},
            "BTC/USD": _pos(),
        }

        with patch.object(fb_state, "load_daily", return_value=None):
            fb_state.reconcile_with_broker(client)

        self.assertEqual(set(fb_state.get_open_positions()), {"MSFT"})


class TestCycleNotificationThrottle(_StateFileMixin, unittest.TestCase):
    def test_first_cycle_of_day_notifies(self):
        self._write(["MSFT", "ANET"])
        self.assertTrue(fb_state.should_notify_cycle(["ANET", "MSFT"]))

    def test_repeat_cycle_same_set_is_suppressed(self):
        """The 6x/day identical message is exactly what this prevents."""
        self._write(["MSFT", "ANET"])
        self.assertTrue(fb_state.should_notify_cycle(["ANET", "MSFT"]))
        for _ in range(5):
            self.assertFalse(fb_state.should_notify_cycle(["ANET", "MSFT"]))

    def test_notifies_when_set_changes(self):
        self._write(["MSFT", "ANET"])
        self.assertTrue(fb_state.should_notify_cycle(["ANET", "MSFT"]))
        # A position closed -> set changed -> notify.
        self.assertTrue(fb_state.should_notify_cycle(["MSFT"]))

    def test_notifies_when_position_added(self):
        self._write(["MSFT"])
        self.assertTrue(fb_state.should_notify_cycle(["MSFT"]))
        self.assertTrue(fb_state.should_notify_cycle(["MSFT", "NVDA"]))

    def test_order_insensitive(self):
        """Same set in a different order is not a change."""
        self._write(["MSFT", "ANET"])
        self.assertTrue(fb_state.should_notify_cycle(["MSFT", "ANET"]))
        self.assertFalse(fb_state.should_notify_cycle(["ANET", "MSFT"]))

    def test_marker_persists_to_state_file(self):
        """The marker must be on disk so it survives to the GCS upload."""
        self._write(["MSFT"])
        fb_state.should_notify_cycle(["MSFT"])
        with open(fb_state.STATE_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertIn("last_cycle_notify", raw)
        self.assertEqual(raw["last_cycle_notify"]["symbols"], ["MSFT"])

    def test_empty_book_notifies_once(self):
        self._write([])
        self.assertTrue(fb_state.should_notify_cycle([]))
        self.assertFalse(fb_state.should_notify_cycle([]))


if __name__ == "__main__":
    unittest.main()
