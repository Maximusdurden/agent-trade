# filename: tests/test_flatbase_stop_and_atr.py
"""Regression tests for two flat-base live-lane defects found 2026-10-14.

1. reconcile_with_broker() looked for an "atr" column that flatbase_state's
   local load_daily() never produces (it returns raw OHLCV). Every adopted
   position therefore silently took the 6% fallback stop, which inflated the
   per-share risk and produced oversized positions (NVDA adopted 220 sh with a
   9.00% stop instead of a 3.77% ATR stop => ~$4.4k risk on a $350-risk config).

2. run_monitor() had no stop-loss execution path. The backtest
   (backtest_flat_base._walk_direction) has always exited on `low <= active_stop`,
   but live only exited on +3R/+5R scale-outs, an SMA20 trail gated behind
   scale_1_filled (+3R), and the 45-day time stop. A position that never reached
   +3R could therefore only exit via the time stop.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload import config_sideload_flatbase as fb_cfg

# Importing sideload.runner_flat_base executes fb_cfg.apply_sideload_overrides()
# at module scope, which mutates core.config globally (SIDELOAD_RESERVED_SYMBOLS,
# TRADING_UNIVERSE, ...). Snapshot and restore so this module does not leak that
# mutation into unrelated tests (e.g. test_universe_guardrail).
_MUTATED_ATTRS = (
    "SIDELOAD_RESERVED_SYMBOLS", "TRADING_UNIVERSE", "SL_SYMBOL",
    "SL_CYCLE_PREFIX", "RISK_PER_TRADE_USD", "MAX_CONCURRENT_POSITIONS",
)
_config_snapshot = {}


def setUpModule():
    from core import config as _cfg
    for attr in _MUTATED_ATTRS:
        if hasattr(_cfg, attr):
            val = getattr(_cfg, attr)
            _config_snapshot[attr] = val.copy() if isinstance(val, (set, list, dict)) else val


def tearDownModule():
    from core import config as _cfg
    for attr, val in _config_snapshot.items():
        setattr(_cfg, attr, val)



def _bars(closes, atr14):
    """Raw OHLCV frame shaped exactly like flatbase_state.load_daily() output."""
    n = len(closes)
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1_000_000] * n,
        },
        index=pd.date_range("2026-01-01", periods=n, freq="D"),
    )


class TestAdoptionUsesAtrNotFallback(unittest.TestCase):
    """reconcile_with_broker must size the stop from ATR14, not a 6% guess."""

    def _adopt(self, entry, atr14):
        from sideload import flatbase_state as fbs

        raw = _bars([entry] * 60, atr14)

        broker_pos = {"qty": "10", "avg_entry_price": str(entry), "is_option": False}
        client = MagicMock()
        client.get_positions.return_value = {"TEST": broker_pos}

        fake_state = {"active_positions": {}, "realized_pnl_30d": 0.0,
                      "bars_held_last_date": None}

        with patch.object(fbs, "load_daily", return_value=raw), \
             patch.object(fbs, "load_state", return_value=fake_state), \
             patch.object(fbs, "save_state"), \
             patch("sideload.backtest_flat_base.add_indicators",
                   side_effect=lambda df, cfg: df.assign(atr14=atr14)):
            adopted = fbs.reconcile_with_broker(client)

        self.assertEqual(adopted, ["TEST"])
        return fake_state["active_positions"]["TEST"]

    def test_stop_uses_atr14_distance(self):
        entry, atr14 = 100.0, 2.0
        rec = self._adopt(entry, atr14)
        expected = entry - fb_cfg.ATR_STOP_MULT * atr14  # 100 - 1.5*2 = 97.0
        self.assertAlmostEqual(rec["stop_loss"], expected, places=6)
        # And explicitly NOT the 6% fallback (94.0).
        self.assertNotAlmostEqual(rec["stop_loss"], entry * 0.94, places=6)

    def test_fallback_only_when_atr_truly_missing(self):
        from sideload import flatbase_state as fbs

        raw = _bars([100.0] * 60, None)
        client = MagicMock()
        client.get_positions.return_value = {
            "TEST": {"qty": "10", "avg_entry_price": "100.0", "is_option": False}
        }
        fake_state = {"active_positions": {}, "realized_pnl_30d": 0.0,
                      "bars_held_last_date": None}
        with patch.object(fbs, "load_daily", return_value=raw), \
             patch.object(fbs, "load_state", return_value=fake_state), \
             patch.object(fbs, "save_state"), \
             patch("sideload.backtest_flat_base.add_indicators",
                   side_effect=lambda df, cfg: df):
            fbs.reconcile_with_broker(client)

        rec = fake_state["active_positions"]["TEST"]
        # The fallback sets ATR = 6% of entry; the stop is entry - 1.5*ATR,
        # i.e. a 9% stop distance (this is why all four adopted positions in
        # production carried an identical 9.00% stop).
        self.assertAlmostEqual(rec["stop_loss"], 100.0 - fb_cfg.ATR_STOP_MULT * 6.0,
                               places=6)
        self.assertAlmostEqual(rec["stop_loss"], 91.0, places=6)


class TestMonitorStopLossExit(unittest.TestCase):
    """run_monitor must SELL when the intraday low breaches stop_loss."""

    def _run_monitor(self, low, stop, entry=100.0, close=None, sma20=None):
        from sideload import runner_flat_base as rfb

        close = entry if close is None else close
        sma20 = close if sma20 is None else sma20

        bar = pd.DataFrame(
            {
                "open": [close], "high": [close], "low": [low],
                "close": [close], "volume": [1_000_000],
                "sma20": [sma20], "atr14": [2.0],
            },
            index=pd.date_range("2026-10-14", periods=1),
        )

        pos = {
            "cluster_id": "FB-TEST-20261014",
            "entry_date": "2026-10-01",
            "bars_held": 5,
            "entry_price": entry,
            "initial_shares": 10.0,
            "remaining_shares": 10.0,
            "stop_loss": stop,
            "target_3r": entry + 30.0,
            "target_5r": entry + 50.0,
            "breakeven_active": False,
            "scale_1_filled": False,
            "scale_2_filled": False,
            "adopted": True,
        }

        client = MagicMock()
        selcalls = []

        with patch.object(rfb, "load_daily", return_value=bar), \
             patch.object(rfb, "add_indicators", side_effect=lambda d, c: d), \
             patch.object(rfb.fb_state, "get_open_positions", return_value={"TEST": pos}), \
             patch.object(rfb.fb_state, "update_position"), \
             patch.object(rfb.fb_state, "close_position") as mock_close, \
             patch.object(rfb.fb_state, "increment_bars_held"), \
             patch.object(rfb, "_log_fill"), \
             patch.object(rfb, "_log_trade_to_db"), \
             patch.object(rfb, "_sell") as mock_sell:
            mock_sell.return_value = {
                "status": "filled", "filled_avg_price": stop, "order_id": "o1"
            }
            rfb.run_monitor(client)
            selcalls = mock_sell.call_args_list
            close_calls = mock_close.call_args_list

        return selcalls, close_calls

    def test_stop_breach_sells_full_remaining_position(self):
        sells, closes = self._run_monitor(low=94.0, stop=97.0)
        self.assertEqual(len(sells), 1, "expected exactly one stop-liquidation sell")
        _, symbol, qty = sells[0].args
        self.assertEqual(symbol, "TEST")
        self.assertEqual(qty, 10.0, "must liquidate the FULL remaining position")
        self.assertEqual(len(closes), 1, "position must be closed in state")

    def test_no_sell_when_low_stays_above_stop(self):
        sells, closes = self._run_monitor(low=98.0, stop=97.0)
        self.assertEqual(len(sells), 0, "must not sell when the stop is not breached")
        self.assertEqual(len(closes), 0)

    def test_stop_beats_scale_out_on_ambiguous_bar(self):
        """A bar that gaps through both the stop and +3R must exit via the stop."""
        entry = 100.0
        # low breaches stop, high would reach +3R: stop must win (checked first).
        from sideload import runner_flat_base as rfb

        bar = pd.DataFrame(
            {
                "open": [entry], "high": [entry + 40.0], "low": [90.0],
                "close": [entry + 30.0], "volume": [1_000_000],
                "sma20": [entry + 30.0], "atr14": [2.0],
            },
            index=pd.date_range("2026-10-14", periods=1),
        )
        pos = {
            "cluster_id": "FB-TEST-20261014", "entry_date": "2026-10-01",
            "bars_held": 5, "entry_price": entry, "initial_shares": 10.0,
            "remaining_shares": 10.0, "stop_loss": 97.0,
            "target_3r": entry + 30.0, "target_5r": entry + 50.0,
            "breakeven_active": False, "scale_1_filled": False,
            "scale_2_filled": False, "adopted": True,
        }
        sold_qty = []

        with patch.object(rfb, "load_daily", return_value=bar), \
             patch.object(rfb, "add_indicators", side_effect=lambda d, c: d), \
             patch.object(rfb.fb_state, "get_open_positions", return_value={"TEST": pos}), \
             patch.object(rfb.fb_state, "update_position"), \
             patch.object(rfb.fb_state, "close_position"), \
             patch.object(rfb.fb_state, "increment_bars_held"), \
             patch.object(rfb, "_log_fill"), \
             patch.object(rfb, "_log_trade_to_db"), \
             patch.object(rfb, "_sell") as mock_sell:
            mock_sell.return_value = {
                "status": "filled", "filled_avg_price": 97.0, "order_id": "o1"
            }
            rfb.run_monitor(MagicMock())
            sold_qty = [c.args[2] for c in mock_sell.call_args_list]

        self.assertEqual(sold_qty, [10.0],
                         "stop must liquidate the full position and preempt +3R scaling")

    def test_dry_run_does_not_sell(self):
        sells, closes = self._run_monitor(low=94.0, stop=97.0)
        # re-run in dry mode
        from sideload import runner_flat_base as rfb
        bar = pd.DataFrame(
            {"open": [94.0], "high": [100.0], "low": [94.0], "close": [94.0],
             "volume": [1_000_000], "sma20": [94.0], "atr14": [2.0]},
            index=pd.date_range("2026-10-14", periods=1),
        )
        pos = {
            "cluster_id": "FB-TEST", "entry_date": "2026-10-01", "bars_held": 5,
            "entry_price": 100.0, "initial_shares": 10.0, "remaining_shares": 10.0,
            "stop_loss": 97.0, "target_3r": 130.0, "target_5r": 150.0,
            "breakeven_active": False, "scale_1_filled": False,
            "scale_2_filled": False, "adopted": True,
        }
        with patch.object(rfb, "load_daily", return_value=bar), \
             patch.object(rfb, "add_indicators", side_effect=lambda d, c: d), \
             patch.object(rfb.fb_state, "get_open_positions", return_value={"TEST": pos}), \
             patch.object(rfb.fb_state, "update_position"), \
             patch.object(rfb.fb_state, "close_position"), \
             patch.object(rfb.fb_state, "increment_bars_held"), \
             patch.object(rfb, "_sell") as mock_sell:
            rfb.run_monitor(MagicMock(), dry_run=True)
        self.assertEqual(mock_sell.call_count, 0, "dry-run must not submit orders")


class TestSwingLaneDeconfliction(unittest.TestCase):
    """The swing lane must not buy a name the flat-base lane already holds.

    NVDA/SMH/MSFT/ANET appear in both universes. On 2026-10-14 flatbase held 118
    NVDA while the swing lane independently bought 102 more, so the account
    carried 220 shares and the extra 102 were managed by neither lane's exits.
    """

    def _candidates(self, symbols):
        return [
            {"symbol": s, "rsi": 5.0, "stretch": 1.0, "close": 100.0,
             "signal_date": "2026-10-14"}
            for s in symbols
        ]

    def test_flatbase_held_symbols_reads_open_positions(self):
        import run_swing_trader as rst
        with patch("sideload.flatbase_state.get_open_positions",
                   return_value={"NVDA": {}, "SMH": {}}):
            self.assertEqual(rst._flatbase_held_symbols(), {"NVDA", "SMH"})

    def test_flatbase_held_symbols_degrades_to_empty_on_error(self):
        """A broken flatbase state must never block the swing lane."""
        import run_swing_trader as rst
        with patch("sideload.flatbase_state.get_open_positions",
                   side_effect=RuntimeError("state unreadable")):
            self.assertEqual(rst._flatbase_held_symbols(), set())

    def test_overlapping_candidate_is_filtered_out(self):
        import run_swing_trader as rst
        cands = self._candidates(["NVDA", "AAPL"])
        fb_held = {"NVDA", "SMH", "MSFT", "ANET"}
        kept = [c for c in cands if c["symbol"] not in fb_held]
        self.assertEqual([c["symbol"] for c in kept], ["AAPL"])

    def test_run_eod_scan_skips_flatbase_held_symbols(self):
        """End-to-end: a flatbase-held candidate never reaches stage_orders."""
        import run_swing_trader as rst
        staged_inputs = []

        def fake_stage(candidates, slots, dry_run=False):
            staged_inputs.extend(c["symbol"] for c in candidates)
            return []

        with patch("core.strategies.swing_rsi2_mean_reversion.scan_signals",
                   return_value=self._candidates(["NVDA", "AAPL"])), \
             patch("core.strategies.swing_rsi2_mean_reversion.stage_orders",
                   side_effect=fake_stage), \
             patch.object(rst, "_load_positions", return_value={}), \
             patch.object(rst, "_opg_window_state", return_value=(True, "open")), \
             patch.object(rst, "_open_order_symbols", return_value=set()), \
             patch.object(rst, "_flatbase_held_symbols",
                          return_value={"NVDA", "SMH", "MSFT", "ANET"}), \
             patch.object(rst, "_log_decision_to_db"), \
             patch("core.alpaca_client.AlpacaClient"):
            rst.run_eod_scan(dry_run=True)

        self.assertNotIn("NVDA", staged_inputs,
                         "flatbase-held NVDA must not be staged by the swing lane")
        self.assertIn("AAPL", staged_inputs)


class TestSwingSellQtyIsLaneScoped(unittest.TestCase):
    """The swing lane must sell only its own shares, not the broker total.

    On 2026-10-14 the broker held 220 NVDA: 118 owned by flatbase and 102 by the
    swing lane. _get_position_qty returned the broker total, so a swing exit
    would have liquidated flatbase's 118 shares as well.
    """

    def _client(self, broker_qty):
        client = MagicMock()
        pos = MagicMock()
        pos.symbol = "NVDA"
        pos.qty = broker_qty
        client.trading_client.get_all_positions.return_value = [pos]
        return client

    def test_sells_lane_qty_not_broker_total(self):
        import run_swing_trader as rst
        qty = rst._get_position_qty(self._client(220.0), "NVDA", fallback=102.0)
        self.assertEqual(qty, 102.0,
                         "must sell the lane's 102 shares, not the broker's 220")

    def test_capped_by_broker_holdings(self):
        """Never sell more than the broker actually holds."""
        import run_swing_trader as rst
        qty = rst._get_position_qty(self._client(50.0), "NVDA", fallback=102.0)
        self.assertEqual(qty, 50.0)

    def test_falls_back_to_broker_total_when_lane_has_no_record(self):
        import run_swing_trader as rst
        qty = rst._get_position_qty(self._client(220.0), "NVDA", fallback=None)
        self.assertEqual(qty, 220.0)

    def test_sole_owner_sells_full_position(self):
        """When no other lane holds the symbol, the full position is sold."""
        import run_swing_trader as rst
        qty = rst._get_position_qty(self._client(17.0), "SMH", fallback=17.0)
        self.assertEqual(qty, 17.0)


if __name__ == "__main__":
    unittest.main()
