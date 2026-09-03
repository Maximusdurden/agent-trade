import sys
import os
import unittest

# Add parent directory of library (Z:\python\projects) and project root to python path
sys.path.insert(0, r"Z:\python\projects")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import config
from core.alpaca_client import AlpacaClient


class TestPartitionSymbols(unittest.TestCase):
    """Verifies OCC option contracts are routed away from the stock bars endpoint."""

    def setUp(self):
        self.client = AlpacaClient.__new__(AlpacaClient)

    def test_partition_routes_option_contracts_separately(self):
        stock, crypto, option = self.client._partition_symbols(
            ["NVDA", "SOL/USD", "NVDA261016C00230000", "BTCUSD"]
        )
        self.assertEqual(stock, ["NVDA"])
        self.assertEqual(crypto, ["SOL/USD", "BTCUSD"])
        self.assertEqual(option, ["NVDA261016C00230000"])

    def test_partition_does_not_misclassify_plain_stocks(self):
        stock, crypto, option = self.client._partition_symbols(
            ["SPY", "AAPL", "MSFT", "QQQ"]
        )
        self.assertEqual(stock, ["SPY", "AAPL", "MSFT", "QQQ"])
        self.assertEqual(crypto, [])
        self.assertEqual(option, [])

    def test_partition_handles_empty(self):
        stock, crypto, option = self.client._partition_symbols([])
        self.assertEqual((stock, crypto, option), ([], [], []))


class TestPaperOptionBarsSkip(unittest.TestCase):
    """Paper accounts can't access OPRA option bars; _fetch_option_data must skip."""

    def _client(self, paper=True):
        c = AlpacaClient.__new__(AlpacaClient)
        c.paper = paper
        c.option_data_client = object()  # a fake non-None client
        c.__dict__.pop("_warned_paper_option_bars", None)
        return c

    def test_paper_skips_option_bars(self):
        c = self._client(paper=True)
        with self.assertLogs("AlpacaClient", level="WARNING") as logs:
            result = c._fetch_option_data(["NVDA261016C00230000"], None, None, 3)
        self.assertEqual(result, [])
        # Should mention the paper/OPRA limitation, not an error.
        self.assertTrue(any("OPRA" in m for m in logs.output), logs.output)

    def test_nonpaper_attempts_fetch(self):
        c = self._client(paper=False)
        # Without an actual client, the option-bars call should attempt and fail
        # gracefully (empty result), but NOT hit the paper short-circuit.
        c.option_data_client = type("NoClient", (), {})()
        result = c._fetch_option_data(["NVDA261016C00230000"], None, None, 3)
        self.assertEqual(result, [])


def test():
    print("Initializing AlpacaClient...")
    client = AlpacaClient()
    print(f"Is mock? {client.is_mock}")
    if client.is_mock:
        print("Mock client is active.")
        return
    
    print("Fetching account state...")
    try:
        acc = client.get_account_state()
        print("Account State:", acc)
    except Exception as e:
        print("Error fetching account state:", e)

    print("Fetching positions...")
    try:
        pos = client.get_positions()
        print("Positions:", pos)
    except Exception as e:
        print("Error fetching positions:", e)


if __name__ == "__main__":
    unittest.main()
