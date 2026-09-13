# filename: tests/test_kill_switch_guard.py
"""Tests for the pre-commit kill-switch guard (.githooks/pre-commit).

The guard refuses to commit a kill_switch.json whose status is HALTED, because
that file is a local cache that tests / GCS syncs can flip to HALTED, and
committing it can halt trading. We test the hook script directly via subprocess
against a scratch git repo so we exercise the real bash logic.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(PROJECT_ROOT, ".githooks", "pre-commit")


def _find_bash():
    """Locate a real bash interpreter.

    On Windows, prefer Git Bash over the WSL launcher stub at
    C:\\Windows\\system32\\bash.exe (which doesn't behave like a normal bash
    for subprocess hooks). Fall back to /bin/bash on POSIX.
    """
    for cand in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ):
        if os.path.exists(cand):
            return cand
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True,
    )


def _make_repo():
    tmp = tempfile.mkdtemp(prefix="ks_guard_")
    _git(tmp, "init", "-q")
    _git(tmp, "config", "user.email", "test@example.com")
    _git(tmp, "config", "user.name", "Test")
    return tmp


def _write_ks(repo, status, updated_by="test"):
    with open(os.path.join(repo, "kill_switch.json"), "w") as f:
        json.dump({"status": status, "updated_by": updated_by}, f)


class TestKillSwitchGuard(unittest.TestCase):
    def setUp(self):
        if not os.path.exists(HOOK):
            self.skipTest("pre-commit hook not present")
        self.bash = _find_bash()
        if not self.bash:
            self.skipTest("bash interpreter not available")
        self.repo = _make_repo()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def _run_hook(self, env_extra=None):
        env = dict(os.environ)
        env["GIT_DIR"] = os.path.join(self.repo, ".git")
        env["GIT_WORK_TREE"] = self.repo
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [self.bash, HOOK], cwd=self.repo, env=env,
            capture_output=True, text=True,
        )

    def test_blocks_halted_commit(self):
        _write_ks(self.repo, "HALTED")
        _git(self.repo, "add", "kill_switch.json")
        r = self._run_hook()
        self.assertNotEqual(r.returncode, 0, "hook should reject HALTED kill switch")
        self.assertIn("HALTED", r.stdout + r.stderr)

    def test_allows_active_commit(self):
        _write_ks(self.repo, "ACTIVE")
        _git(self.repo, "add", "kill_switch.json")
        r = self._run_hook()
        self.assertEqual(r.returncode, 0, "hook should allow ACTIVE kill switch")

    def test_allows_halted_with_override(self):
        _write_ks(self.repo, "HALTED")
        _git(self.repo, "add", "kill_switch.json")
        r = self._run_hook({"ALLOW_HALTED_KILL_SWITCH": "1"})
        self.assertEqual(r.returncode, 0, "override should allow HALTED kill switch")

    def test_noop_when_ks_not_staged(self):
        # A staged change to an unrelated file must not trip the guard.
        with open(os.path.join(self.repo, "foo.txt"), "w") as f:
            f.write("hi")
        _git(self.repo, "add", "foo.txt")
        r = self._run_hook()
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)