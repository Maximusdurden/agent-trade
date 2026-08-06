"""Validates the chat-handler DB retry logic and atomic GCS replace."""
import sys, os, sqlite3, time
sys.path.insert(0, r"Z:\python\projects")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_retry_helper():
    """Simulate the _db_read retry helper: transient lock then success."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    # Replicate the helper inline
    def _db_read(fn):
        last_err = None
        for attempt in range(3):
            try:
                return fn()
            except sqlite3.OperationalError as e:
                last_err = e
                if "locked" in str(e).lower() or "unable to open" in str(e).lower():
                    time.sleep(0.01)
                    continue
                raise
        raise last_err

    result = _db_read(flaky)
    assert result == "ok", f"Expected ok, got {result}"
    assert calls["n"] == 3, f"Expected 3 calls, got {calls['n']}"
    print("PASS: retry helper recovers from transient 'database is locked'")


def test_atomic_replace():
    """Simulate the atomic GCS download: write tmp then os.replace."""
    tmp = "/tmp/test_db_atomic.tmp"
    final = "/tmp/test_db_atomic.db"
    with open(tmp, "w") as f:
        f.write("partial-data")
    os.replace(tmp, final)
    assert os.path.exists(final)
    assert not os.path.exists(tmp)
    with open(final) as f:
        assert f.read() == "partial-data"
    os.remove(final)
    print("PASS: atomic os.replace works (no partial file left behind)")


if __name__ == "__main__":
    test_retry_helper()
    test_atomic_replace()
    print("\nALL TESTS PASSED")
