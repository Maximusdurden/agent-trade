import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_FILENAME"] = "test_strategist_ab_notify.db"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.strategist_ab_notify import should_notify, load_state, save_state  # noqa: E402
from tools.strategist_ab_report import analyze, build_round_trips, load_strategy_models, MODEL_RE  # noqa: E402
from core.database import init_db, get_system_state  # noqa: E402


class TestABNotifyThrottle(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)

    def test_force_always_notifies(self):
        self.assertTrue(should_notify([], {"last_sent_at": self.now.isoformat()}, self.now, force=True))

    def test_new_trips_notify(self):
        self.assertTrue(should_notify(["trip"], {}, self.now))

    def test_first_ever_heartbeat_notifies(self):
        self.assertTrue(should_notify([], {}, self.now))

    def test_quiet_and_recent_does_not_notify(self):
        state = {"last_sent_at": self.now.isoformat()}
        self.assertFalse(should_notify([], state, self.now))

    def test_weekly_heartbeat_due_after_7_days(self):
        state = {"last_sent_at": (self.now - timedelta(days=8)).isoformat()}
        self.assertTrue(should_notify([], state, self.now))


class TestABNotifyStatePersistence(unittest.TestCase):
    """State must survive across executions (Cloud Run ephemeral FS) via system_state."""

    def test_save_then_load_roundtrips(self):
        init_db()
        save_state({"last_sent_at": "2026-09-02T12:00:00+00:00",
                    "last_notified_open_ts": "2026-09-01T10:00:00+00:00"})
        state = load_state()
        self.assertEqual(state.get("last_sent_at"), "2026-09-02T12:00:00+00:00")
        self.assertEqual(state.get("last_notified_open_ts"), "2026-09-01T10:00:00+00:00")

    def test_state_persists_in_system_state_table(self):
        init_db()
        save_state({"last_sent_at": "2026-09-02T12:00:00+00:00"})
        self.assertEqual(get_system_state("ab_notify_last_sent_at"), "2026-09-02T12:00:00+00:00")


class TestABModelTagParsing(unittest.TestCase):
    def test_model_regex_parses_tag(self):
        m = MODEL_RE.search("v20260902|model=anthropic-claude-sonnet-5")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "anthropic-claude-sonnet-5")

    def test_model_regex_handles_deepseek(self):
        m = MODEL_RE.search("v20260902|model=deepseek-deepseek-r1")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "deepseek-deepseek-r1")


if __name__ == "__main__":
    unittest.main(verbosity=2)