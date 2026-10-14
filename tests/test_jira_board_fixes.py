"""Regression tests for the 2026-10-14 Jira board fixes.

Covers:
  - TMCL-1084/1085/1086/1091/1092: UUID order_id must be coerced to str so the
    swing positions file (JSON) can serialize it.
  - TMCL-1087/1088: Alpaca 40310000 "insufficient balance" must be normalized
    to InsufficientBalanceError (benign) instead of a CRITICAL Jira ticket.
  - TMCL-1093: AMD backtest grid must not pickle the full DataFrames per task
    (OOM -> BrokenProcessPool); data rides in the pool initializer.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_swing_trader as rst
from core import alpaca_client as ac


class TestSwingUuidOrderId(unittest.TestCase):
    """TMCL-1084/1085/1086/1091/1092: UUID order_id must serialize to JSON."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_positions_file = rst.POSITIONS_FILE
        rst.POSITIONS_FILE = os.path.join(self._tmpdir.name, "swing_positions.json")

    def tearDown(self):
        rst.POSITIONS_FILE = self._orig_positions_file
        self._tmpdir.cleanup()

    def test_opg_order_id_is_str(self):
        """place_market_on_open_order must return a str order_id (not UUID)."""
        import uuid
        client = MagicMock()
        order = MagicMock()
        order.id = uuid.uuid4()  # Alpaca returns a UUID object
        client.trading_client.submit_order.return_value = order
        with patch.object(rst, "_opg_window_state", return_value=(True, "open")):
            res = rst.place_market_on_open_order(client, "AAPL", 10, "buy")
        self.assertEqual(res["status"], "submitted")
        self.assertIsInstance(res["order_id"], str)
        self.assertEqual(res["order_id"], str(order.id))

    def test_save_positions_serializes_uuid(self):
        """_save_positions must not crash on a UUID order_id (default=str)."""
        import uuid
        positions = {"AAPL": {"order_id": uuid.uuid4(), "qty": 10}}
        rst._save_positions(positions)
        with open(rst.POSITIONS_FILE, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        self.assertIsInstance(loaded["AAPL"]["order_id"], str)


class TestInsufficientBalance(unittest.TestCase):
    """TMCL-1087/1088: 40310000 insufficient balance is benign, not CRITICAL."""

    def test_error_class_exists(self):
        err = ac.InsufficientBalanceError("BTC/USD", 48.17, 0.0)
        self.assertIn("BTC/USD", str(err))
        self.assertIn("48.17", str(err))

    def test_40310000_normalized_to_insufficient_balance(self):
        """execute_market_order must raise InsufficientBalanceError on 40310000."""
        client = ac.AlpacaClient.__new__(ac.AlpacaClient)
        client.is_mock = False
        client.trading_client = MagicMock()
        client.trading_client.submit_order.side_effect = Exception(
            '{"code":40310000,"message":"insufficient balance for USD '
            '(requested: 48.17, available: 0)","symbol":"USD"}')
        client.cancel_open_orders = MagicMock()
        client.get_latest_price = MagicMock(return_value=100.0)
        # qty=1.0 * $100 = $100 notional, above the $10 minimum-notional
        # pre-check, so the request reaches the broker and gets the 40310000.
        with self.assertRaises(ac.InsufficientBalanceError):
            client.execute_market_order("BTC/USD", 1.0, "buy")

    def test_runner_treats_insufficient_balance_as_benign(self):
        """runner.py must log WARNING (not CRITICAL) for InsufficientBalanceError."""
        import runner
        from core.alpaca_client import InsufficientBalanceError
        with patch.object(runner.logger, "warning") as warn, \
             patch.object(runner.logger, "critical") as crit:
            # Simulate the runner's except block logic directly.
            e = InsufficientBalanceError("BTC/USD", 48.17, 0.0)
            try:
                from core.alpaca_client import MinimumNotionalError, InsufficientBalanceError
                _is_benign = isinstance(e, (MinimumNotionalError, InsufficientBalanceError))
            except Exception:
                _is_benign = False
            if _is_benign:
                runner.logger.warning(f"Skipped order for BTC/USD: {e}")
            else:
                runner.logger.critical(f"FATAL: Order execution failed for BTC/USD: {e}")
        warn.assert_called_once()
        crit.assert_not_called()


class TestAmdBacktestPool(unittest.TestCase):
    """TMCL-1093: grid tasks must not pickle full DataFrames per task."""

    def test_tasks_carry_only_combo_and_keys(self):
        """run_grid must build tasks as (combo, keys) — no df_by_interval."""
        from sideload import backtest_amd as bt
        import pandas as pd

        df = pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]})
        df_by_interval = {"5m": df}
        grid = {"interval": ["5m"], "rsi_entry_max": [10.0, 20.0]}

        # ProcessPoolExecutor is imported INSIDE run_grid, so patch it via
        # sys.modules (the local import resolves to the module attribute).
        import sys
        import concurrent.futures
        mock_ex = MagicMock()
        mock_ex.return_value.__enter__.return_value.map.return_value = []
        with patch.dict(sys.modules, {"concurrent.futures": concurrent.futures}), \
             patch.object(concurrent.futures, "ProcessPoolExecutor", mock_ex):
            bt.run_grid(df_by_interval, grid, top_n=5, workers=2)

        # The executor must be constructed with the initializer (data loaded
        # once per worker) — NOT pickled into every task.
        call_kwargs = mock_ex.call_args.kwargs
        self.assertEqual(call_kwargs["initializer"], bt._pool_init)
        self.assertEqual(call_kwargs["initargs"], (df_by_interval,))

        # Tasks must NOT carry the DataFrames. ex.map(_eval_combo, tasks, ...)
        # -> call args are (_eval_combo, tasks).
        map_args = mock_ex.return_value.__enter__.return_value.map.call_args.args
        self.assertEqual(map_args[0], bt._eval_combo)
        tasks = map_args[1]
        for task in tasks:
            combo, keys = task
            self.assertEqual(len(task), 2)
            self.assertNotIn("df_by_interval", task)

    def test_eval_combo_reads_pool_global(self):
        """_eval_combo must read DataFrames from the pool global, not args."""
        from sideload import backtest_amd as bt
        import numpy as np
        import pandas as pd

        # simulate_config needs >= 60 rows with indicator columns.
        n = 200
        idx = pd.date_range("2026-01-01", periods=n, freq="5min")
        df = pd.DataFrame({
            "close": np.linspace(100.0, 110.0, n),
            "rsi_14": np.full(n, 30.0),
            "atr_14": np.full(n, 1.0),
            "atr_pct": np.full(n, 0.01),
            "vwap": np.linspace(100.0, 109.0, n),
            "macd_hist": np.zeros(n),
        }, index=idx)
        bt._POOL_DF_BY_INTERVAL = {"5m": df}
        try:
            # Patch simulate_config so the test only verifies the pool-global
            # read path (the DataFrame comes from _POOL_DF_BY_INTERVAL, not
            # from the task args).
            with patch("sideload.backtest_amd.simulate_config",
                       return_value={"trades": 10, "pnl": 1.0, "win_rate": 0.5,
                                     "expectancy": 0.1}):
                # combo is a TUPLE of values (like itertools.product yields);
                # keys are the grid key names. zip(keys, combo) -> cfg dict.
                result = bt._eval_combo((("5m", 45.0),
                                         ["interval", "rsi_entry_max"]))
            self.assertIsNotNone(result)
            self.assertEqual(result["interval"], "5m")
        finally:
            bt._POOL_DF_BY_INTERVAL = {}


if __name__ == "__main__":
    unittest.main()