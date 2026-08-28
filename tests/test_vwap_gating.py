import sys
import os
import unittest
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.data_provider import DataProvider
from core import config


def _make_bars(n_today: int, n_prior: int = 30, timeframe_min: int = 15) -> pd.DataFrame:
    """Build a single-symbol intraday bar frame with `n_today` bars on the latest
    day and `n_prior` bars on the preceding day(s)."""
    end = pd.Timestamp.now().tz_localize("America/New_York").normalize()
    # Prior-day bars (yesterday)
    prior_start = end - pd.Timedelta(days=1)
    prior_idx = pd.date_range(prior_start, periods=n_prior, freq=f"{timeframe_min}min")
    # Today's bars
    today_idx = pd.date_range(end, periods=n_today, freq=f"{timeframe_min}min")

    idx = prior_idx.append(today_idx)
    n = len(idx)
    base = 100.0
    closes = base + np.linspace(0, 1.0, n)
    highs = closes + 0.5
    lows = closes - 0.5
    opens = closes - 0.1
    volumes = np.full(n, 1000.0)

    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return df


class TestCountTodayBars(unittest.TestCase):
    def setUp(self):
        self.provider = DataProvider.__new__(DataProvider)  # bypass __init__

    def test_single_bar_today(self):
        df = _make_bars(n_today=1)
        self.assertEqual(self.provider._count_today_bars(df), 1)

    def test_four_bars_today(self):
        df = _make_bars(n_today=4)
        self.assertEqual(self.provider._count_today_bars(df), 4)

    def test_empty_frame(self):
        self.assertEqual(self.provider._count_today_bars(pd.DataFrame()), 0)

    def test_multiindex_uses_last_symbol(self):
        df = _make_bars(n_today=3)
        df.index.name = "timestamp"
        df["symbol"] = "JNJ"
        df = df.set_index("symbol", append=True).reorder_levels(["symbol", "timestamp"])
        self.assertEqual(self.provider._count_today_bars(df), 3)


class TestVwapGating(unittest.TestCase):
    def setUp(self):
        self.provider = DataProvider.__new__(DataProvider)
        self._orig_min = config.MIN_VWAP_BARS

    def tearDown(self):
        config.MIN_VWAP_BARS = self._orig_min

    def _market_state(self, n_today):
        df = _make_bars(n_today=n_today)
        df = self.provider._add_technical_indicators_single(df)
        latest = df.iloc[-1]
        # Build the same market_state dict the real method constructs.
        vwap_valid = self.provider._count_today_bars(df) >= config.MIN_VWAP_BARS
        return {
            "vwap": float(latest["vwap"]) if (vwap_valid and not pd.isna(latest["vwap"])) else None,
            "vwap_dist_pct": float(latest["vwap_dist_pct"]) if (vwap_valid and not pd.isna(latest["vwap_dist_pct"])) else None,
            "vwap_upper_1": float(latest["vwap_upper_1"]) if (vwap_valid and not pd.isna(latest["vwap_upper_1"])) else None,
        }

    def test_vwap_gated_below_min(self):
        config.MIN_VWAP_BARS = 4
        ms = self._market_state(n_today=1)
        self.assertIsNone(ms["vwap"])
        self.assertIsNone(ms["vwap_dist_pct"])
        self.assertIsNone(ms["vwap_upper_1"])

    def test_vwap_exposed_at_min(self):
        config.MIN_VWAP_BARS = 4
        ms = self._market_state(n_today=4)
        self.assertIsNotNone(ms["vwap"])
        self.assertIsNotNone(ms["vwap_dist_pct"])
        self.assertIsNotNone(ms["vwap_upper_1"])

    def test_vwap_exposed_above_min(self):
        config.MIN_VWAP_BARS = 4
        ms = self._market_state(n_today=8)
        self.assertIsNotNone(ms["vwap"])
        self.assertIsNotNone(ms["vwap_dist_pct"])


if __name__ == "__main__":
    unittest.main()