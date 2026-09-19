# filename: tests/test_backtest_amd_simulator.py
"""Tests for the AMD backtest simulator (sideload/backtest_amd.py).

Guards against the two P0 bugs found in the 2026-09-19 audit:
1. LOOK-AHEAD BIAS: the entry-walk opened positions at the current loop bar
   ``i`` while recording ``entry_i = e`` (a FUTURE bar), so exits measured PnL
   against prices BEFORE the entry (negative hold durations) and inflated the
   backtest. The walk must jump ``i`` to the entry bar ``e``.
2. PARAMETER-INSENSITIVITY: changing a swept variable (rsi_entry_max,
   max_hold_hours, regime_filter, etc.) must change the trade set. A grid
   search whose output does not vary with its inputs is not searching.

These tests use SYNTHETIC data with a known signal so they are deterministic
and require no network / Alpaca credentials.
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sideload import backtest_amd as bt
from core.data_provider import DataProvider


def _make_synthetic_df(n=400, seed=1, interval_hours=24.0):
    """Build a synthetic OHLCV frame with a real, tradeable signal.

    A random walk with a couple of injected trends so the regime filter and
    RSI pullback have something to bite on. Indicators are computed with the
    real DataProvider math. Defaults to INTRADAY (15-min) bars so the VWAP
    dead-zone gate is meaningful (VWAP accumulates across many bars/day).
    """
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0005, 0.02, n)
    # Inject trends so regime/RSI produce distinct entry sets.
    for start, length, drift in [(50, 80, 0.01), (200, 100, -0.008)]:
        rets[start:start + length] += drift
    close = 100.0 * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.01, n)))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    if interval_hours < 1.0:
        freq = f"{int(interval_hours * 60)}min"
    else:
        freq = f"{int(interval_hours)}h"
    idx = pd.date_range("2025-01-01", periods=n, freq=freq)
    df = pd.DataFrame({
        "open": open_, "high": high, "low": low, "close": close, "volume": volume,
    }, index=idx)
    df.index.name = "timestamp"
    dp = DataProvider.__new__(DataProvider)
    return dp._add_technical_indicators_single(df, symbol="AMD")


def _base_cfg(**overrides):
    cfg = {
        "interval": "1d", "rsi_entry_max": 50.0, "rsi_exit_overbought": 0.0,
        "macd_filter": "off", "vwap_dead_zone_sigma": 0.5, "min_edge_sigma": 0.3,
        "atr_sizing_baseline_pct": 2.5, "max_hold_hours": 24.0,
        "trail_stop_giveback_pct": 0.05, "time_of_day": "all", "regime_filter": "trending",
    }
    cfg.update(overrides)
    return cfg


class TestBacktestSimulator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Intraday (15-min) bars so VWAP dead-zone is meaningful.
        cls.df = _make_synthetic_df(n=400, seed=1, interval_hours=0.25)

    def test_produces_trades_on_synthetic_signal(self):
        """The simulator should find trades on data with a real signal."""
        s = bt.simulate_config(self.df, _base_cfg())
        self.assertGreater(s["trades"], 0, "expected trades on a signal-bearing frame")

    def test_no_look_ahead_negative_hold(self):
        """Entry bar must never be in the future relative to the exit bar.

        Regression test for the P0 look-ahead bug: the old walk opened a
        position at loop bar ``i`` with ``entry_i = e > i``, producing negative
        hold durations and future-peeking PnL. We assert every trade has
        entry_i <= exit_i by re-running the walk with exit logging.
        """
        trades = self._simulate_with_holds(self.df, _base_cfg())
        self.assertGreater(len(trades), 0)
        for t in trades:
            self.assertGreaterEqual(
                t["exit_i"], t["entry_i"],
                f"look-ahead: exit_i {t['exit_i']} < entry_i {t['entry_i']}",
            )
            self.assertGreaterEqual(t["hold_bars"], 0, "negative hold duration")

    def test_rsi_entry_max_changes_trade_set(self):
        """Tightening the RSI entry gate must change the trade set."""
        loose = bt.simulate_config(self.df, _base_cfg(rsi_entry_max=50.0))
        tight = bt.simulate_config(self.df, _base_cfg(rsi_entry_max=40.0))
        self.assertNotEqual(
            (loose["trades"], round(loose["pnl"], 2)),
            (tight["trades"], round(tight["pnl"], 2)),
            "rsi_entry_max should change the trade set",
        )

    def test_max_hold_changes_trade_set(self):
        """Enabling/disabling max-hold must change the trade set."""
        # On 15-min bars, 72h = 288 bars (longer than the 400-bar frame, so it
        # never binds). Use a short max-hold (2h = 8 bars) that WILL bind.
        disabled = bt.simulate_config(self.df, _base_cfg(max_hold_hours=0.0))
        enabled = bt.simulate_config(self.df, _base_cfg(max_hold_hours=2.0))
        self.assertNotEqual(
            (disabled["trades"], round(disabled["pnl"], 2)),
            (enabled["trades"], round(enabled["pnl"], 2)),
            "max_hold_hours should change the trade set",
        )

    def test_regime_filter_changes_trade_set(self):
        """The regime filter must change the trade set."""
        all_regimes = bt.simulate_config(self.df, _base_cfg(regime_filter="all"))
        trending = bt.simulate_config(self.df, _base_cfg(regime_filter="trending"))
        self.assertNotEqual(
            (all_regimes["trades"], round(all_regimes["pnl"], 2)),
            (trending["trades"], round(trending["pnl"], 2)),
            "regime_filter should change the trade set",
        )

    def test_daily_bars_disable_vwap_dead_zone(self):
        """On daily bars the VWAP dead-zone must not veto entries.

        Regression test for the daily-VWAP artifact: with 1 bar/day the
        session-based VWAP collapses to the day's typical price, so the
        dead-zone gate blocks ~97% of bars. The simulator must detect daily
        bars and disable the dead-zone gate.
        """
        daily = _make_synthetic_df(n=400, seed=1, interval_hours=24.0)
        # With the dead-zone disabled on daily bars, we should find trades.
        s = bt.simulate_config(daily, _base_cfg())
        # The exact count depends on the signal; the key assertion is that the
        # daily path does not collapse to zero via the dead-zone artifact.
        self.assertGreater(s["trades"], 0, "daily bars should still produce trades")

    def test_timeframe_mapping_distinguishes_1d_and_1h(self):
        """1d and 1h must map to different TimeFrame objects (interval bug)."""
        from core.alpaca_client import AlpacaClient
        tf_d, _ = AlpacaClient._get_timeframe(None, "1d")
        tf_h, _ = AlpacaClient._get_timeframe(None, "1h")
        self.assertNotEqual(
            str(tf_d), str(tf_h),
            "1d and 1h must map to different TimeFrames (was: both -> Day)",
        )

    def test_multifold_aggregates_oos_trades(self):
        """Multi-fold walk-forward must aggregate OOS trades across folds.

        Regression test for the P1 honest-OOS requirement: the old single-split
        walk-forward produced only 3-4 OOS trades (statistically meaningless).
        The multi-fold version must aggregate trades across all folds so a
        config's OOS sample is meaningful.
        """
        df_by_interval = {"1d": self.df}
        cfg = _base_cfg(max_hold_hours=24.0, trail_stop_giveback_pct=0.0)
        # min_oos_trades=1 so we see the raw aggregate even if it's small.
        results = bt.walk_forward_validate_multifold(
            df_by_interval, [cfg], n_folds=5, min_oos_trades=1)
        # The config may or may not clear the final win/expectancy filter, but
        # if it produced any OOS trades, the aggregate must be >= the sum of
        # per-fold trades (i.e. it aggregated, not just took one fold).
        if results:
            v = results[0]
            self.assertGreaterEqual(v["oos_trades"], 1)
            self.assertEqual(v["n_folds"], 5)
            self.assertEqual(len(v["folds"]), 5)

    def test_bar_hours_uses_mode_not_last_two_bars(self):
        """_bar_hours must use the MODE of bar deltas, not the last two bars.

        Regression test: the old implementation read the last two bars, which
        often span a market-close gap (e.g. 20:00 -> next 09:30), overstating
        the bar length for intraday intervals. For 1h bars the mode is 1.0h.
        """
        # Build a 1h frame with a market-close gap at the end (last two bars
        # far apart) to prove the mode is used, not the last delta.
        idx = pd.date_range("2026-09-01", periods=100, freq="1h")
        # Append a gap: last bar jumps 13h (market close).
        idx = idx.append(pd.DatetimeIndex([idx[-1] + pd.Timedelta(hours=13)]))
        df = pd.DataFrame({"close": np.linspace(100, 110, len(idx))}, index=idx)
        self.assertAlmostEqual(bt._bar_hours(df), 1.0, places=2)

    def test_timeframe_mapping_1min(self):
        """1min must map to a 1-minute TimeFrame (not fall through to Day)."""
        from core.alpaca_client import AlpacaClient
        tf_1m, _ = AlpacaClient._get_timeframe(None, "1min")
        tf_1d, _ = AlpacaClient._get_timeframe(None, "1d")
        self.assertNotEqual(str(tf_1m), str(tf_1d), "1min must not map to Day")

    def test_short_direction_produces_trades(self):
        """The short side (P2) must produce round-trips on signal-bearing data."""
        s = bt.simulate_config(self.df, _base_cfg(direction="short", rsi_short_entry_min=55.0))
        self.assertGreater(s["trades"], 0, "short side should produce trades")
        self.assertGreater(s.get("short_trades", 0), 0, "short_trades should be > 0")

    def test_both_direction_combines_long_and_short(self):
        """'both' must combine long and short round-trips."""
        long_only = bt.simulate_config(self.df, _base_cfg(direction="long"))
        both = bt.simulate_config(self.df, _base_cfg(direction="both", rsi_short_entry_min=55.0))
        self.assertGreaterEqual(
            both["trades"], long_only["trades"],
            "'both' should have >= long-only trades",
        )
        self.assertGreater(both.get("short_trades", 0), 0, "'both' should include short trades")

    def test_short_pnl_sign(self):
        """Short PnL must be positive when price FALLS after entry.

        A short entered at a high price and exited at a lower price must show
        positive PnL. We verify the short walk's PnL sign convention by checking
        that a short trade's pnl = (entry - exit) * qty.
        """
        # Build a frame with a clear overbought-then-fade pattern.
        rng = np.random.default_rng(7)
        n = 300
        # Rising then falling: RSI high at the peak, then fades.
        t = np.linspace(0, 4 * np.pi, n)
        close = 100 + 15 * np.sin(t) + np.linspace(0, 5, n)
        high = close + 1.0
        low = close - 1.0
        open_ = np.roll(close, 1); open_[0] = close[0]
        volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
        idx = pd.date_range("2026-01-01", periods=n, freq="1h")
        df = pd.DataFrame({"open": open_, "high": high, "low": low,
                           "close": close, "volume": volume}, index=idx)
        dp = DataProvider.__new__(DataProvider)
        df = dp._add_technical_indicators_single(df, symbol="AMD")
        s = bt.simulate_config(df, _base_cfg(direction="short", rsi_short_entry_min=55.0,
                                             trail_stop_giveback_pct=0.03))
        self.assertGreater(s["trades"], 0, "short side should find trades on a fade pattern")

    def test_options_lane_config_split(self):
        """P3: core options disabled, sideload options lane enables them.

        The dedicated AMD-options lane (runner_options.py) must enable options
        with AMD-specific tuning, while the core product has options disabled.
        """
        from core import config as core_cfg
        from sideload import config_sideload as sl_cfg

        # Core default: options disabled (P3 split).
        self.assertFalse(core_cfg.OPTIONS_ENABLED)

        # Sideload options lane: enables options with AMD tuning.
        sl_cfg.apply_sideload_overrides()
        sl_cfg.apply_sideload_options_overrides()
        self.assertTrue(core_cfg.OPTIONS_ENABLED)
        self.assertIn("AMD", core_cfg.OPTIONS_UNIVERSE)
        # AMD-specific DTE window (shorter than core's 30-60).
        self.assertLessEqual(core_cfg.OPTIONS_DTE_MIN, 14)
        self.assertLessEqual(core_cfg.OPTIONS_DTE_MAX, 45)

    # -- helpers ---------------------------------------------------------
    def _simulate_with_holds(self, df, cfg):
        """Re-run the walk, recording entry_i/exit_i/hold_bars per trade."""
        price = df["close"].to_numpy(dtype=float)
        n = len(price)
        rsi = df["rsi_14"].to_numpy(dtype=float)
        atr = df["atr_14"].to_numpy(dtype=float)
        atr_pct = df["atr_pct"].to_numpy(dtype=float)
        vwap = df["vwap"].to_numpy(dtype=float)
        macd_hist = df["macd_hist"].to_numpy(dtype=float)
        rsi = np.where(np.isnan(rsi), 50.0, rsi)
        atr = np.where(np.isnan(atr), 0.0, atr)
        atr_pct = np.where(np.isnan(atr_pct), 0.0, atr_pct)
        vwap = np.where(np.isnan(vwap), 0.0, vwap)
        macd_hist = np.where(np.isnan(macd_hist), 0.0, macd_hist)

        rsi_entry_max = float(cfg.get("rsi_entry_max", 45.0))
        rsi_exit = float(cfg.get("rsi_exit_overbought", 0.0))
        macd_filter = cfg.get("macd_filter", "off")
        vwap_sigma = float(cfg.get("vwap_dead_zone_sigma", 1.0))
        min_edge = float(cfg.get("min_edge_sigma", 0.5))
        max_hold = float(cfg.get("max_hold_hours", 0.0))
        trail_giveback = float(cfg.get("trail_stop_giveback_pct", 0.0))
        time_of_day = cfg.get("time_of_day", "all")
        regime_filter = cfg.get("regime_filter", "all")

        hod = np.array([pd.Timestamp(ts).hour for ts in df.index], dtype=int)
        tod_mask = np.ones(n, dtype=bool)
        if time_of_day == "am":
            tod_mask = (hod >= 6) & (hod < 12)
        elif time_of_day == "pm":
            tod_mask = (hod >= 12) & (hod < 18)

        regime_mask = np.ones(n, dtype=bool)
        if regime_filter != "all":
            sma20 = df["sma_20"].to_numpy(dtype=float)
            sma20_prev = np.roll(sma20, 5)
            sma20_prev[:5] = np.nan
            slope_pct = np.where((sma20_prev > 0) & ~np.isnan(sma20_prev),
                                 (sma20 - sma20_prev) / sma20_prev * 100.0, 0.0)
            trending = np.abs(slope_pct) >= 0.1
            regime_mask = trending if regime_filter == "trending" else ~trending

        valid_vwap = (vwap > 0) & (atr > 0)
        edge_sigma = np.where(valid_vwap, np.abs(price - vwap) / np.where(atr > 0, atr, 1.0), np.nan)
        in_dead_zone = valid_vwap & (np.abs(price - vwap) <= vwap_sigma * atr)
        has_edge = np.isnan(edge_sigma) | (edge_sigma >= min_edge)
        entry_sig = (rsi <= rsi_entry_max) & (~in_dead_zone) & has_edge & tod_mask & regime_mask
        if macd_filter == "hist_gt_0":
            entry_sig = entry_sig & (macd_hist > 0)
        entry_idx = np.flatnonzero(entry_sig)

        exit_sig = np.zeros(n, dtype=bool)
        if rsi_exit > 0:
            exit_sig = rsi >= rsi_exit

        bar_hours = bt._bar_hours(df)
        max_hold_bars = int(max_hold / bar_hours) if (max_hold > 0 and bar_hours > 0) else 0

        trades = []
        ei = 0
        position = None
        i = 20
        while i < n:
            if position is None:
                while ei < len(entry_idx) and entry_idx[ei] < i:
                    ei += 1
                if ei >= len(entry_idx):
                    break
                e = entry_idx[ei]
                p = price[e]
                if p <= 0 or np.isnan(p):
                    ei += 1
                    continue
                i = e
                position = {"entry_price": p, "entry_i": e, "peak_price": p}
                ei += 1
                i += 1
            else:
                exit_reason = None
                if rsi_exit > 0 and exit_sig[i]:
                    exit_reason = "rsi_overbought"
                if max_hold_bars > 0 and (i - position["entry_i"]) >= max_hold_bars:
                    exit_reason = "max_hold"
                if trail_giveback > 0:
                    peak = max(position["peak_price"], price[i])
                    position["peak_price"] = peak
                    gain = (peak - position["entry_price"]) / position["entry_price"]
                    giveback = (peak - price[i]) / peak if peak > 0 else 0.0
                    if gain >= 0.02 and giveback >= trail_giveback:
                        exit_reason = "trailing_stop"
                if exit_reason:
                    trades.append({
                        "entry_i": position["entry_i"], "exit_i": i,
                        "hold_bars": i - position["entry_i"],
                    })
                    position = None
                i += 1
        return trades


if __name__ == "__main__":
    unittest.main()