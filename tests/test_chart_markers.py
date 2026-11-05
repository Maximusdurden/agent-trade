"""Regression tests: trade charts must always show BOTH buy and sell markers.

Covers the 2026-11-04 AMD blog chart, where the buy marker was generated but
invisible for two independent reasons:

1. The marker was plotted at the raw fill price, which sits INSIDE its own
   candle body (AMD buy 479.48 inside a 476.81-499.23 body), so the green
   triangle was buried in a green candle. The sell at 504.48 happened to land
   near its candle's top, which is why only the sell appeared.
2. ``_fetch_bars`` anchored its lookback to ``datetime.now()`` instead of the
   trade window, so a multi-day hold never fetched bars for the entry day and
   there was no candle to attach the BUY marker to.

These tests are network-free: the broker fill feed is monkeypatched.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
import pandas as pd

from core import charts


def _bars(rows):
    """Build a bars DataFrame shaped like _calculate_indicators output."""
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame(
        {
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[4] for r in rows],
            "Volume": [1000] * len(rows),
        },
        index=idx,
    )


# ---------------------------------------------------------------------------
# _marker_y: markers must sit OUTSIDE the candle body
# ---------------------------------------------------------------------------
def test_buy_marker_pushed_below_candle_low():
    """The 11-04 AMD case: fill 479.48 inside a 476.81-499.23 body."""
    df = _bars([("2026-11-04 09:35", 477.13, 499.23, 476.805, 499.08)])
    y = charts._marker_y(df, 0, 479.48, "buy")
    assert y < 476.805, f"buy marker {y} must be below the candle low"
    assert not (476.805 <= y <= 499.23), "buy marker must not be buried in the body"


def test_sell_marker_pushed_above_candle_high():
    df = _bars([("2026-11-04 09:35", 477.13, 499.23, 476.805, 499.08)])
    y = charts._marker_y(df, 0, 495.0, "sell")
    assert y > 499.23, f"sell marker {y} must be above the candle high"


def test_marker_already_outside_body_is_not_pulled_inside():
    df = _bars([("2026-11-04 09:35", 477.13, 499.23, 476.805, 499.08)])
    assert charts._marker_y(df, 0, 470.0, "buy") <= 470.0
    assert charts._marker_y(df, 0, 505.0, "sell") >= 505.0


def test_marker_y_is_defensive_on_bad_index():
    df = _bars([("2026-11-04 09:35", 477.13, 499.23, 476.805, 499.08)])
    # Out-of-range index must fall back to the raw price, not raise.
    assert charts._marker_y(df, 99, 479.48, "buy") == 479.48


# ---------------------------------------------------------------------------
# _prepare_overlays: both sides always produce a marker + annotation
# ---------------------------------------------------------------------------
def _overlays(monkeypatch, df, fills, round_trips=None):
    monkeypatch.setattr(charts, "_fetch_order_fills", lambda *a, **k: fills)
    start = df.index.min().to_pydatetime()
    end = df.index.max().to_pydatetime()
    return charts._prepare_overlays(None, df, "AMD", start, end, round_trips or [])


def test_both_markers_rendered_for_intraday_round_trip(monkeypatch):
    df = _bars([
        ("2026-11-04 09:30", 484.0, 485.5, 483.5, 485.0),
        ("2026-11-04 09:35", 477.13, 499.23, 476.805, 499.08),
        ("2026-11-04 09:40", 499.0, 505.0, 498.0, 504.0),
    ])
    fills = [
        {"ts": pd.Timestamp("2026-11-04 09:35", tz="America/New_York"),
         "side": "buy", "qty": 17.0, "price": 479.48},
        {"ts": pd.Timestamp("2026-11-04 09:40", tz="America/New_York"),
         "side": "sell", "qty": 17.0, "price": 504.48},
    ]
    ap, legends, annotations = _overlays(monkeypatch, df, fills)

    buys = [a for a in annotations if "BUY" in a["text"]]
    sells = [a for a in annotations if "SELL" in a["text"]]
    assert len(buys) == 1, f"expected 1 BUY annotation, got {len(buys)}"
    assert len(sells) == 1, f"expected 1 SELL annotation, got {len(sells)}"

    # Annotation text reports the TRUE fill price...
    assert "@479.48" in buys[0]["text"]
    assert "@504.48" in sells[0]["text"]

    # ...while the plotted marker sits outside the candle body.
    row = df.iloc[buys[0]["x"]]
    assert not (row["Low"] <= buys[0]["y"] <= row["High"]), (
        f"buy marker y={buys[0]['y']} buried in body {row['Low']}-{row['High']}"
    )

    # Both scatter addplots must be present (one per side).
    assert len(ap) >= 2


def test_round_trip_fallback_also_offsets_markers(monkeypatch):
    """When the broker feed returns nothing, the round-trip fallback must still
    place both markers outside the candle body."""
    df = _bars([
        ("2026-11-04 09:35", 477.13, 499.23, 476.805, 499.08),
        ("2026-11-04 09:40", 499.0, 505.0, 498.0, 504.0),
    ])
    trips = [{
        "open_ts": "2026-11-04T14:35:00+00:00",
        "close_ts": "2026-11-04T14:40:00+00:00",
        "qty": 17.0, "entry_price": 479.48, "exit_price": 504.48,
    }]
    _, _, annotations = _overlays(monkeypatch, df, [], trips)

    buys = [a for a in annotations if "BUY" in a["text"]]
    sells = [a for a in annotations if "SELL" in a["text"]]
    assert len(buys) == 1 and len(sells) == 1
    row = df.iloc[buys[0]["x"]]
    assert not (row["Low"] <= buys[0]["y"] <= row["High"])


# ---------------------------------------------------------------------------
# Multi-day: the bar window must be anchored to the trade, not to "now"
# ---------------------------------------------------------------------------
def test_fetch_bars_requests_trade_window_not_now(monkeypatch):
    """A multi-day hold must request bars covering the ENTRY day.

    The old implementation derived its lookback from ``datetime.now()``, so a
    trade older than a few days requested a window ending today and the entry
    day's bars were never returned.
    """
    captured = {}

    class FakeClient:
        def get_historical_bars(self, symbol, limit=100, timeframe_str="day",
                                max_retries=3, start=None, end=None):
            captured["start"] = start
            captured["end"] = end
            captured["limit"] = limit
            return pd.DataFrame()

    entry = pd.Timestamp("2026-09-28 15:48", tz="America/New_York")
    exit_ = pd.Timestamp("2026-11-03 09:32", tz="America/New_York")
    charts._fetch_bars(FakeClient(), "MSFT", entry, exit_)

    assert captured["start"] is not None and captured["end"] is not None, (
        "get_historical_bars must receive an explicit start/end window"
    )
    # The requested window must reach back to (or before) the entry day.
    assert captured["start"].date() <= entry.date(), (
        f"window start {captured['start']} does not cover entry {entry.date()}"
    )
    assert captured["end"].date() >= exit_.date(), (
        f"window end {captured['end']} does not cover exit {exit_.date()}"
    )
