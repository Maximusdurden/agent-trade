"""Tests that the FIFO cost-basis override does NOT corrupt option positions.

Regression test for the bug where ``_apply_fifo_cost_basis_override`` applied
the crypto cost-basis correction to option positions, computing
``unrealized = market_value - qty*price`` (missing the 100x contract
multiplier) and producing wildly wrong option PnL on the dashboard.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, r"Z:\python\projects")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.alpaca_client import AlpacaClient


class TestFifoOverrideSkipsOptions(unittest.TestCase):
    def _make_client(self):
        client = AlpacaClient.__new__(AlpacaClient)
        client.is_mock = False
        return client

    def test_option_position_pnl_not_overridden(self):
        client = self._make_client()
        # Simulate a live option position whose Alpaca unrealized_pl is -325.
        positions = {
            "AAPL261009C00320000": {
                "qty": 5.0,
                "qty_available": 5.0,
                "market_value": 3975.0,
                "avg_entry_price": 8.6,
                "unrealized_pnl": -325.0,
                "is_option": True,
            }
        }
        # The order-history FIFO basis would (incorrectly) compute cost_basis=43
        # for this option if it were not skipped.
        basis = {
            "AAPL261009C00320000": {
                "qty": 5.0,
                "avg_entry_price": 8.6,
                "cost_basis": 43.0,
            }
        }
        with patch.object(client, "_compute_cost_basis_from_orders", return_value=basis):
            client._apply_fifo_cost_basis_override(positions)

        # The option's PnL must remain Alpaca's authoritative value (-325),
        # NOT the corrupted market_value - cost_basis = 3975 - 43 = 3932.
        self.assertEqual(positions["AAPL261009C00320000"]["unrealized_pnl"], -325.0)
        self.assertEqual(positions["AAPL261009C00320000"]["avg_entry_price"], 8.6)

    def test_crypto_position_still_overridden(self):
        client = self._make_client()
        positions = {
            "SOL/USD": {
                "qty": 57.73,
                "qty_available": 57.73,
                "market_value": 6195.0,
                "avg_entry_price": 46.2,
                "unrealized_pnl": 3428.0,
            }
        }
        basis = {
            "SOL/USD": {
                "qty": 57.73,
                "avg_entry_price": 108.97,
                "cost_basis": 6290.0,
            }
        }
        with patch.object(client, "_compute_cost_basis_from_orders", return_value=basis):
            client._apply_fifo_cost_basis_override(positions)

        # Crypto should still be corrected: unrealized = market_value - cost_basis.
        self.assertAlmostEqual(positions["SOL/USD"]["unrealized_pnl"], 6195.0 - 6290.0, places=2)
        self.assertAlmostEqual(positions["SOL/USD"]["avg_entry_price"], 108.97, places=2)


if __name__ == "__main__":
    unittest.main()