#!/usr/bin/env python3
"""Smoke tests for the Dexter blog layer (agent-trade).

Offline sanity checks so deployment can be verified fast. These do NOT hit
WordPress, Discord, or the LLM — they exercise the deterministic mapping/parse
logic that a deployment would otherwise only reach at runtime.

Run: python -m tests.test_blog_smoke
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class BlogStatsSmoke(unittest.TestCase):
    def test_empty_df_returns_zeros(self):
        from core import blog_stats as bs
        import pandas as pd
        df = pd.DataFrame(columns=["ticker", "entry_date", "exit_date", "qty",
                                   "pnl_dollar", "pnl_percent", "hold_time_minutes"])
        stats = bs.calculate_dashboard_stats(df, today="2026-09-04")
        self.assertEqual(stats["pnl_7d"], 0.0)
        self.assertEqual(stats["est_balance"], bs.START_BALANCE)
        self.assertEqual(bs.get_last_10_days_performance(df), [False] * 10)

    def test_round_trip_to_dexter_df(self):
        from core import blog_stats as bs
        trips = [{
            "symbol": "AAPL", "open_ts": "2026-09-01T14:00:00Z",
            "close_ts": "2026-09-03T19:30:00Z", "qty": 10,
            "entry_price": 100.0, "exit_price": 105.0,
            "pnl": 50.0, "pnl_pct": 5.0, "holding_hours": 53.5, "win": True,
        }]
        df = bs.round_trips_to_dexter_df(trips)
        self.assertEqual(df.iloc[0]["ticker"], "AAPL")
        self.assertEqual(df.iloc[0]["pnl_dollar"], 50.0)
        self.assertAlmostEqual(df.iloc[0]["hold_time_minutes"], 53.5 * 60, places=3)


class BridgeSmoke(unittest.TestCase):
    def test_round_trip_to_row(self):
        from tools.build_blog_db import round_trip_to_row
        rt = {
            "symbol": "SOL/USD", "open_ts": "2026-09-01T10:00:00Z",
            "close_ts": "2026-09-02T20:00:00Z", "qty": 5,
            "entry_price": 100.0, "exit_price": 110.0,
            "pnl": 50.0, "pnl_pct": 10.0, "holding_hours": 34.0, "win": True,
        }
        row = round_trip_to_row(rt)
        self.assertEqual(row["ticker"], "SOL/USD")
        self.assertEqual(row["pnl_dollar"], 50.0)
        self.assertEqual(row["hold_time_minutes"], 34.0 * 60)
        self.assertIn(row["exit_date"], ("2026-09-02",))  # ET date, defensively

    def test_write_mirror_preserves_ids(self):
        """The mirror must upsert on the natural key, preserving row ids so
        realized_trade_grades.trade_id stays linked across re-runs."""
        import os
        import sqlite3
        import tempfile
        from tools.build_blog_db import write_mirror

        trips = [{
            "symbol": "AAPL", "open_ts": "2026-09-01T10:00:00Z",
            "close_ts": "2026-09-02T20:00:00Z", "qty": 2,
            "entry_price": 100.0, "exit_price": 102.0,
            "pnl": 4.0, "pnl_pct": 2.0, "holding_hours": 10.0, "win": True,
        }]
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            write_mirror(trips, tmp.name)
            # Re-run (simulates next blog run) — id should stay the same.
            write_mirror(trips, tmp.name)
            conn = sqlite3.connect(tmp.name)
            ids = [r[0] for r in conn.execute("SELECT id FROM realized_trades").fetchall()]
            conn.close()
            self.assertEqual(len(ids), 1)
            self.assertEqual(ids[0], 1)
        finally:
            os.unlink(tmp.name)


class WordpressSmoke(unittest.TestCase):
    def test_autolink_tickers(self):
        from core import wordpress as wp
        text = "We bought AAPL today."
        out = wp.autolink_tickers(text, ["AAPL"])
        self.assertIn("<a href=", out)
        self.assertIn("AAPL", out)

    def test_disclaimer(self):
        from core import wordpress as wp
        d = wp.get_disclaimer_html()
        self.assertIn("Paper Trading", d)
        self.assertIn("Dexter", d)


class SEOSmoke(unittest.TestCase):
    def test_json_extractor_handles_noise(self):
        from core.seo import _extract_json_object
        s = 'ok:\n{"meta_title":"T","meta_description":"D","tags":[],"json_ld_schema":{}}\n\nby the way!'
        obj = _extract_json_object(s)
        self.assertIsNotNone(obj)
        self.assertEqual(obj["meta_title"], "T")


class GradingSmoke(unittest.TestCase):
    def test_letter_grade(self):
        from core.grader import _letter_grade
        self.assertEqual(_letter_grade(96), "A+")
        self.assertEqual(_letter_grade(85), "B")
        self.assertEqual(_letter_grade(55), "F")


if __name__ == "__main__":
    unittest.main(verbosity=2)