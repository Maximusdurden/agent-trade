# filename: tests/test_health_check_local_tasks.py
"""Local Windows scheduled tasks must be visible to the health check.

check_schedulers() only inspects Cloud Scheduler. The quote sniffer and the EOD
DB sync are LOCAL Windows tasks, so they were invisible — and both were
silently dead for weeks:

  - ModelAQuoteSniffer: action was `cmd.exe` with no `/c`, so it opened an
    interactive shell, never ran the script, and hung until the execution time
    limit killed it (0xC000013A). It had never recorded a single row.
  - ModelAOptions EOD DB Sync: the registration script existed but had never
    been run, so the task did not exist at all.

Two blind spots these tests pin:
  1. A task that has NEVER run has no LastRunTime. The cloud check skips such
     jobs entirely; here it must be reported.
  2. A task whose last result is a failure code must be reported even though
     the task state still reads "Ready".
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import run_health_check as hc


def _ps_output(rows):
    """Build the pipe-delimited stdout the PowerShell snippet produces."""
    return "\n".join(f"{n}|{s}|{lr}|{r}" for n, s, lr, r in rows)


def _run_with(rows, os_name="nt"):
    fake = MagicMock()
    fake.stdout = _ps_output(rows)
    with patch.object(hc.os, "name", os_name), \
         patch("subprocess.run", return_value=fake):
        return hc.check_local_tasks()


def _recent(hours=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


class TestLocalTaskCheck(unittest.TestCase):
    def test_skipped_on_non_windows(self):
        """Cloud Run (Linux) must be unaffected."""
        self.assertEqual(_run_with([], os_name="posix"), [])

    def test_missing_required_task_is_reported(self):
        """The exact failure that hid the EOD DB sync for weeks."""
        issues = _run_with([
            ("ModelAQuoteSniffer", "Ready", _recent(), "0"),
        ])
        self.assertTrue(any("MISSING" in i and "EOD DB Sync" in i for i in issues))

    def test_never_run_task_is_reported(self):
        """A task with no LastRunTime must not be silently skipped."""
        issues = _run_with([
            ("ModelAQuoteSniffer", "Ready", "", "267011"),
            ("ModelAOptions EOD DB Sync", "Ready", _recent(), "0"),
        ])
        self.assertTrue(any("NEVER RAN" in i and "QuoteSniffer" in i for i in issues))

    def test_failed_result_is_reported(self):
        """0xC000013A (control-C exit) is the sniffer's historical failure."""
        issues = _run_with([
            ("ModelAQuoteSniffer", "Ready", _recent(), "3221225786"),
            ("ModelAOptions EOD DB Sync", "Ready", _recent(), "0"),
        ])
        self.assertTrue(any("FAILED" in i and "QuoteSniffer" in i for i in issues))

    def test_disabled_task_is_reported(self):
        issues = _run_with([
            ("ModelAQuoteSniffer", "Disabled", _recent(), "0"),
            ("ModelAOptions EOD DB Sync", "Ready", _recent(), "0"),
        ])
        self.assertTrue(any("DISABLED" in i and "QuoteSniffer" in i for i in issues))

    def test_stale_task_is_reported(self):
        issues = _run_with([
            ("ModelAQuoteSniffer", "Ready", _recent(hours=100), "0"),
            ("ModelAOptions EOD DB Sync", "Ready", _recent(), "0"),
        ])
        self.assertTrue(any("STALE" in i and "QuoteSniffer" in i for i in issues))

    def test_healthy_tasks_produce_no_issues(self):
        issues = _run_with([
            ("ModelAQuoteSniffer", "Ready", _recent(), "0"),
            ("ModelAOptions EOD DB Sync", "Ready", _recent(), "0"),
        ])
        self.assertEqual(issues, [])

    def test_not_yet_run_code_is_not_a_failure(self):
        """267011 = SCHED_S_TASK_HAS_NOT_RUN, a benign code."""
        issues = _run_with([
            ("ModelAQuoteSniffer", "Ready", _recent(), "267011"),
            ("ModelAOptions EOD DB Sync", "Ready", _recent(), "0"),
        ])
        self.assertEqual(issues, [])

    def test_powershell_failure_does_not_raise(self):
        with patch.object(hc.os, "name", "nt"), \
             patch("subprocess.run", side_effect=OSError("no powershell")):
            self.assertEqual(hc.check_local_tasks(), [])

    def test_empty_enumeration_does_not_raise(self):
        """If PowerShell returns nothing, do not report every task missing."""
        self.assertEqual(_run_with([]), [])


if __name__ == "__main__":
    unittest.main()
