# filename: tests/test_regime_edge.py
"""Tests for the Phase 1/2 edge work: regime classifier, normalized edge, and
the guardrail minimum-edge gate.

- data_provider.classify_regime: deterministic, volatility-aware regime labels.
- data_provider.normalized_edge_sigma: |vwap_dist| / ATR.
- guardrails._min_edge_sigma_reason: blocks BUY/SELL inside the noise band.
"""
import os
import sys
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.data_provider import classify_regime, normalized_edge_sigma
from core.guardrails import RiskGuardrails


def _make_frame(closes, highs=None, lows=None, volumes=None):
    """Build a synthetic OHLC frame with the indicators the classifier needs."""
    n = len(closes)
    highs = highs if highs is not None else [c * 1.01 for c in closes]
    lows = lows if lows is not None else [c * 0.99 for c in closes]
    volumes = volumes if volumes is not None else [1000] * n
    # VWAP groups by date, so the frame needs a DatetimeIndex.
    idx = pd.date_range("2026-09-01", periods=n, freq="15min")
    df = pd.DataFrame({
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    }, index=idx)
    # Reuse the real indicator math so the classifier sees realistic columns.
    from core.data_provider import DataProvider
    dp = DataProvider.__new__(DataProvider)  # no __init__ side effects
    return DataProvider._add_technical_indicators_single(dp, df)


class TestClassifyRegime(unittest.TestCase):
    def test_uptrend_classified(self):
        # A noisy but sustained uptrend. Per-bar drift (0.2) is small enough that
        # the 3-bar move stays under the breakout threshold (3*0.2=0.6 < 2*ATR),
        # but the 10-bar SMA-20 slope (0.2/100 = 0.2%/bar) exceeds the trend
        # threshold (0.10%/bar).
        rng = np.random.default_rng(1)
        closes = [100 + i * 0.2 + rng.normal(0, 0.3) for i in range(60)]
        df = _make_frame(closes)
        self.assertEqual(classify_regime(df), "TRENDING_UP")

    def test_downtrend_classified(self):
        rng = np.random.default_rng(2)
        closes = [100 - i * 0.2 + rng.normal(0, 0.3) for i in range(60)]
        df = _make_frame(closes)
        self.assertEqual(classify_regime(df), "TRENDING_DOWN")

    def test_flat_range_classified(self):
        # Flat closes with small noise -> RANGING.
        rng = np.random.default_rng(42)
        closes = [100 + rng.normal(0, 0.1) for _ in range(60)]
        df = _make_frame(closes)
        self.assertEqual(classify_regime(df), "RANGING")

    def test_breakout_classified(self):
        # A large single-day jump far from VWAP -> BREAKOUT.
        closes = [100] * 40 + [110]
        df = _make_frame(closes)
        self.assertEqual(classify_regime(df), "BREAKOUT")

    def test_insufficient_data_fails_safe(self):
        self.assertEqual(classify_regime(pd.DataFrame()), "RANGING")
        self.assertEqual(classify_regime(None), "RANGING")


class TestNormalizedEdgeSigma(unittest.TestCase):
    def test_edge_returns_positive_value(self):
        closes = [100] * 40 + [110]
        df = _make_frame(closes)
        edge = normalized_edge_sigma(df)
        self.assertIsNotNone(edge)
        self.assertGreater(edge, 0.0)

    def test_edge_none_on_insufficient_data(self):
        self.assertIsNone(normalized_edge_sigma(pd.DataFrame()))
        self.assertIsNone(normalized_edge_sigma(None))


class TestMinEdgeSigmaGuardrail(unittest.TestCase):
    def setUp(self):
        self.g = RiskGuardrails()

    def test_blocks_low_edge(self):
        # edge_sigma 0.2 < MIN_EDGE_SIGMA 0.5 -> blocked.
        with patch.object(__import__("core.config", fromlist=["config"]), "MIN_EDGE_SIGMA", 0.5):
            reason = self.g._min_edge_sigma_reason("AAPL", "BUY", {"edge_sigma": 0.2})
        self.assertIsNotNone(reason)
        self.assertIn("noise band", reason)

    def test_allows_high_edge(self):
        with patch.object(__import__("core.config", fromlist=["config"]), "MIN_EDGE_SIGMA", 0.5):
            reason = self.g._min_edge_sigma_reason("AAPL", "BUY", {"edge_sigma": 1.2})
        self.assertIsNone(reason)

    def test_noop_when_edge_none(self):
        # VWAP gated -> no edge floor.
        with patch.object(__import__("core.config", fromlist=["config"]), "MIN_EDGE_SIGMA", 0.5):
            reason = self.g._min_edge_sigma_reason("AAPL", "BUY", {"edge_sigma": None})
        self.assertIsNone(reason)

    def test_noop_when_disabled(self):
        with patch.object(__import__("core.config", fromlist=["config"]), "MIN_EDGE_SIGMA", 0.0):
            reason = self.g._min_edge_sigma_reason("AAPL", "BUY", {"edge_sigma": 0.1})
        self.assertIsNone(reason)

    def test_noop_for_hold(self):
        with patch.object(__import__("core.config", fromlist=["config"]), "MIN_EDGE_SIGMA", 0.5):
            reason = self.g._min_edge_sigma_reason("AAPL", "HOLD", {"edge_sigma": 0.1})
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)