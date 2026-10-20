"""Regression tests for the state-file corruption self-heal (TMCL incident 2026-10-13).

The swing lane's `swing_positions.json` was corrupted (144-byte truncated JSON)
and the lane ran blind all day: every run logged "Could not load positions
file", treated positions as empty, and re-uploaded the same bad bytes to GCS.
These tests pin the self-heal behavior:

  - `run_swing_trader._load_positions()` quarantines a corrupt file and writes
    a fresh empty state (so the next GCS upload overwrites the bad blob).
  - `sideload.flatbase_state.load_state()` does the same for flat-base state.
  - `sideload.flatbase_state.save_state()` writes atomically (no torn writes).
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_swing_trader as rst
from sideload import flatbase_state as fbs


class TestSwingLoadPositionsSelfHeal(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_positions_file = rst.POSITIONS_FILE
        rst.POSITIONS_FILE = os.path.join(self._tmpdir.name, "swing_positions.json")

    def tearDown(self):
        rst.POSITIONS_FILE = self._orig_positions_file
        self._tmpdir.cleanup()

    def _write(self, content: str):
        with open(rst.POSITIONS_FILE, "w", encoding="utf-8") as fh:
            fh.write(content)

    def test_loads_valid_json(self):
        self._write(json.dumps({"NVDA": {"qty": 118}}))
        self.assertEqual(rst._load_positions(), {"NVDA": {"qty": 118}})

    def test_missing_file_returns_empty(self):
        self.assertEqual(rst._load_positions(), {})

    def test_corrupt_file_quarantines_and_writes_fresh(self):
        # The exact 144-byte truncation from the 2026-10-12/13 incident.
        self._write('{\n  "SMH": {\n    "entry_ts": "2026-10-12T19:10:31.556346-04:00",\n'
                    '    "signal_date": "2026-10-09",\n    "rsi": 6.775318981639302,\n'
                    '    "order_id": ')
        with patch.object(rst, "_now_et", return_value=rst._now_et()):
            result = rst._load_positions()
        self.assertEqual(result, {})
        # The corrupt file must be quarantined (not deleted).
        quarantined = [f for f in os.listdir(self._tmpdir.name)
                       if f.startswith("swing_positions.json.corrupt-")]
        self.assertEqual(len(quarantined), 1)
        # A fresh valid empty state must exist so GCS gets overwritten.
        with open(rst.POSITIONS_FILE, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {})

    def test_corrupt_file_does_not_raise(self):
        self._write("{not valid json")
        with patch.object(rst, "_now_et", return_value=rst._now_et()):
            result = rst._load_positions()  # must not raise
        self.assertEqual(result, {})


class TestFlatbaseLoadStateSelfHeal(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_state_path = fbs.STATE_PATH
        fbs.STATE_PATH = os.path.join(self._tmpdir.name, "flatbase_positions.json")

    def tearDown(self):
        fbs.STATE_PATH = self._orig_state_path
        self._tmpdir.cleanup()

    def _write(self, content: str):
        with open(fbs.STATE_PATH, "w", encoding="utf-8") as fh:
            fh.write(content)

    def test_loads_valid_state(self):
        self._write(json.dumps({"active_positions": {"NVDA": {}}, "realized_pnl_30d": 0.0}))
        state = fbs.load_state()
        self.assertIn("NVDA", state["active_positions"])

    def test_missing_state_returns_empty(self):
        state = fbs.load_state()
        self.assertEqual(state, {"active_positions": {}, "realized_pnl_30d": 0.0})

    def test_corrupt_state_quarantines_and_writes_fresh(self):
        self._write('{"active_positions": {"NVDA": ')
        state = fbs.load_state()
        self.assertEqual(state["active_positions"], {})
        quarantined = [f for f in os.listdir(self._tmpdir.name)
                       if f.startswith("flatbase_positions.json.corrupt-")]
        self.assertEqual(len(quarantined), 1)
        with open(fbs.STATE_PATH, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"active_positions": {}, "realized_pnl_30d": 0.0})

    def test_save_state_is_atomic(self):
        # After a save, no .tmp file should remain and the state must parse.
        fbs.save_state({"active_positions": {"NVDA": {"qty": 118}}, "realized_pnl_30d": 1.0})
        self.assertFalse(os.path.exists(fbs.STATE_PATH + ".tmp"))
        with open(fbs.STATE_PATH, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["active_positions"]["NVDA"]["qty"], 118)


if __name__ == "__main__":
    unittest.main()
