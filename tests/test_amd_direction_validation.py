"""Tests for the AMD directional analysis dataset validation (TMCL-1013..1015).

Regression: ``analyze_descriptive`` / ``write_report`` index required columns
(``amplitude_pct``, ``direction``, etc.) directly. When ``--analyze`` loaded a
stale/older dataset missing those columns, it crashed with a bare
``KeyError: 'amplitude_pct'`` (the TMCL-1013..1015 signature). The fix adds
``_validate_analysis_columns`` so a mismatched dataset fails with a clear,
actionable error instead.
"""

import os
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sideload.analyze_amd_direction import (
    REQUIRED_ANALYSIS_COLUMNS,
    _validate_analysis_columns,
)


def _make_valid_df() -> pd.DataFrame:
    """A minimal frame containing every required analysis column."""
    cols = REQUIRED_ANALYSIS_COLUMNS
    data = {c: [0.0] * 5 for c in cols}
    data["direction"] = ["higher", "lower", "higher", "lower", "higher"]
    df = pd.DataFrame(data)
    df.attrs["symbol"] = "AMD"
    return df


class TestValidateAnalysisColumns(unittest.TestCase):
    def test_valid_dataset_passes(self):
        df = _make_valid_df()
        # Should not raise.
        _validate_analysis_columns(df, "AMD", "close")

    def test_missing_amplitude_pct_raises_clear_error(self):
        # The exact TMCL-1013..1015 failure: amplitude_pct missing.
        df = _make_valid_df().drop(columns=["amplitude_pct"])
        with self.assertRaises(ValueError) as ctx:
            _validate_analysis_columns(df, "AMD", "close")
        msg = str(ctx.exception)
        self.assertIn("amplitude_pct", msg)
        self.assertIn("AMD", msg)
        self.assertIn("--build-dataset", msg)

    def test_missing_direction_raises(self):
        df = _make_valid_df().drop(columns=["direction"])
        with self.assertRaises(ValueError):
            _validate_analysis_columns(df, "AMD", "close")

    def test_multiple_missing_columns_reported(self):
        df = _make_valid_df().drop(columns=["amplitude_pct", "abs_move_sigma", "regime"])
        with self.assertRaises(ValueError) as ctx:
            _validate_analysis_columns(df, "AMD", "close")
        msg = str(ctx.exception)
        for col in ("amplitude_pct", "abs_move_sigma", "regime"):
            self.assertIn(col, msg)

    def test_empty_frame_raises(self):
        with self.assertRaises(ValueError):
            _validate_analysis_columns(pd.DataFrame(), "AMD", "close")


if __name__ == "__main__":
    unittest.main()