"""Tests for daily strategy snapshotting + stale-rule regeneration.

Verifies:
1. `snapshot_all_strategies` archives the currently-in-force rule per ticker.
2. `get_strategy_snapshot` returns a date-keyed {TICKER: rule/hint/version} map.
3. Snapshot is idempotent per (date, ticker).
4. `ensure_active_strategy` treats an OLD rule as stale and regenerates it.
5. `ensure_active_strategy` keeps a fresh rule as-is (no unnecessary refresh).
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, r"Z:\python\projects")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import database
from runner import ensure_active_strategy


class TestStrategySnapshotAndStaleness(unittest.TestCase):
    def setUp(self):
        with database.get_db_connection() as conn:
            conn.execute("DELETE FROM strategy_history")
            conn.execute("DELETE FROM strategy_snapshots")
            conn.execute("DELETE FROM system_state")
            conn.commit()

    def _log_rule(self, ticker, rule, hint=None, ts=None):
        if ts:
            with database.get_db_connection() as conn:
                conn.execute(
                    "INSERT INTO strategy_history (timestamp, ticker, yesterdays_rules, todays_rules, meta_reasoning, instrument_hint) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (ts, ticker.upper(), "old", rule, "meta", hint))
                conn.commit()
        else:
            database.log_strategy_history(ticker, "old", rule, "meta", instrument_hint=hint)

    # --- Snapshot tests ---
    def test_snapshot_all_strategies(self):
        self._log_rule("NVDA", "nvda rule", hint="option")
        self._log_rule("COST", "cost rule")
        n = database.snapshot_all_strategies("2026-09-07")
        snap = database.get_strategy_snapshot("2026-09-07")
        self.assertEqual(n, 2)
        self.assertEqual(snap["NVDA"]["rule"], "nvda rule")
        self.assertEqual(snap["NVDA"]["instrument_hint"], "option")
        self.assertEqual(snap["COST"]["rule"], "cost rule")

    def test_snapshot_idempotent_per_date(self):
        self._log_rule("AAPL", "aapl rule v1")
        database.snapshot_all_strategies("2026-09-07")
        # Update the rule, re-snapshot same date -> should replace, still 1 row.
        self._log_rule("AAPL", "aapl rule v2")
        n = database.snapshot_all_strategies("2026-09-07")
        snap = database.get_strategy_snapshot("2026-09-07")
        self.assertEqual(n, 1)
        self.assertEqual(snap["AAPL"]["rule"], "aapl rule v2")

    def test_get_snapshot_empty_when_none(self):
        snap = database.get_strategy_snapshot("2026-01-01")
        self.assertEqual(snap, {})

    # --- Staleness tests ---
    def _ensure(self, symbol):
        with patch("core.strategist.MetaStrategist") as mock_ms:
            inst = mock_ms.return_value
            inst.run_single_ticker_refinement.return_value = None
            ok = ensure_active_strategy(symbol, None)
        return ok

    def test_stale_rule_triggers_refresh(self):
        # Rule written 100h ago (stale, > 26h default) -> refresh IS triggered.
        self._log_rule("MSFT", "old stale rule", ts="2026-09-01T00:00:00")
        with patch("core.strategist.MetaStrategist") as mock_ms:
            inst = mock_ms.return_value
            inst.run_single_ticker_refinement.return_value = None
            ok = ensure_active_strategy("MSFT", None)
        # Refresh attempted; rule still valid after (stale rule remains valid) -> True.
        self.assertTrue(ok)
        mock_ms.assert_called_once()

    def test_fresh_rule_no_refresh(self):
        self._log_rule("JNJ", "fresh rule", ts="2026-09-08T10:00:00")
        with patch("core.strategist.MetaStrategist") as mock_ms:
            ok = ensure_active_strategy("JNJ", None)
        self.assertTrue(ok)
        mock_ms.assert_not_called()

    def test_missing_rule_triggers_refresh(self):
        with patch("core.strategist.MetaStrategist") as mock_ms:
            inst = mock_ms.return_value
            inst.run_single_ticker_refinement.return_value = None
            ok = ensure_active_strategy("ZZZZ", None)
        # Missing rule -> refresh attempted; not repaired -> False.
        self.assertFalse(ok)
        mock_ms.assert_called_once()


if __name__ == "__main__":
    unittest.main()
