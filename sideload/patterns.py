#!/usr/bin/env python3
"""Hand-rolled candlestick pattern detectors for the sideload lane.

Implements the 5 most promising patterns from ``research_candlestick.md``
(engulfing, hammer/hanging man, morning/evening star, three soldiers/crows,
abandoned baby) as pure-pandas detectors — no TA-Lib dependency.

Design notes:
  - Each detector takes a DataFrame with at least ``open, high, low, close``
    columns (and ``volume`` where noted) and returns a boolean Series aligned
    to the input index. True at index i means the pattern COMPLETED at bar i.
  - Thresholds follow the canonical definitions (Nison 1991; Morris 2006) with
    the standard relaxations used in practice (Bulkowski 2008).
  - Detectors are vectorized (no per-row Python loops) so they run fast on
    years of daily bars.

Usage:
    from sideload.patterns import detect_all_patterns
    signals = detect_all_patterns(df)   # dict[str, pd.Series[bool]]
"""

from __future__ import annotations

import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# Thresholds (canonical, with practical relaxations)
# ---------------------------------------------------------------------------
# Body must be at least this fraction of the bar's total range to count as a
# "real" body (vs a doji). Used to distinguish long bodies from indecision.
MIN_BODY_FRACTION = 0.30

# A "long" body is at least this multiple of the average body over the window.
LONG_BODY_MULT = 1.5

# Shadow must be at least this multiple of the body to count as "long".
LONG_SHADOW_MULT = 2.0

# A doji body is at most this fraction of the bar's total range.
DOJI_BODY_FRACTION = 0.10

# Window for the average body (rolling mean of |close - open|).
BODY_AVG_WINDOW = 20

# Minimum bars required before a signal can fire (warm-up for rolling stats).
MIN_BARS = BODY_AVG_WINDOW + 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _body(df: pd.DataFrame) -> pd.Series:
    """Real body size (absolute)."""
    return (df["close"] - df["open"]).abs()


def _body_signed(df: pd.DataFrame) -> pd.Series:
    """Signed body: positive = bullish, negative = bearish."""
    return df["close"] - df["open"]


def _range(df: pd.DataFrame) -> pd.Series:
    """Full bar range (high - low)."""
    return df["high"] - df["low"]


def _upper_shadow(df: pd.DataFrame) -> pd.Series:
    """Upper shadow length (high - max(open, close))."""
    return df["high"] - df[["open", "close"]].max(axis=1)


def _lower_shadow(df: pd.DataFrame) -> pd.Series:
    """Lower shadow length (min(open, close) - low)."""
    return df[["open", "close"]].min(axis=1) - df["low"]


def _is_bullish(df: pd.DataFrame) -> pd.Series:
    return df["close"] > df["open"]


def _is_bearish(df: pd.DataFrame) -> pd.Series:
    return df["close"] < df["open"]


def _is_doji(df: pd.DataFrame) -> pd.Series:
    """Open ≈ close (body <= DOJI_BODY_FRACTION of range)."""
    rng = _range(df)
    return (_body(df) <= DOJI_BODY_FRACTION * rng) & (rng > 0)


def _avg_body(df: pd.DataFrame) -> pd.Series:
    """Rolling average body size (for 'long body' comparisons)."""
    return _body(df).rolling(BODY_AVG_WINDOW, min_periods=5).mean()


def _is_long_body(df: pd.DataFrame) -> pd.Series:
    """Body >= LONG_BODY_MULT * average body."""
    avg = _avg_body(df)
    return (_body(df) >= LONG_BODY_MULT * avg) & (avg > 0)


def _prior_trend(df: pd.DataFrame, bars: int = 5, direction: str = "down",
                 offset: int = 0) -> pd.Series:
    """True where the PRIOR `bars` bars (before current, minus `offset`) were
    trending.

    direction="down": the close fell over the prior window (close[i-bars-offset]
    > close[i-1-offset]). direction="up": the close rose. Uses the close-to-close
    move over the window, which is the standard trend precondition for reversal
    patterns.

    ``offset`` lets callers look PAST the pattern's own bars. For a 3-candle
    pattern completing at bar i, the trend must be measured on the window BEFORE
    the pattern started (offset=2 for a 3-candle pattern, offset=1 for a 2-candle
    pattern). Without this, the pattern's own candles (which move against the
    prior trend) contaminate the trend measurement.
    """
    if direction == "down":
        return df["close"].shift(bars + offset) > df["close"].shift(1 + offset)
    return df["close"].shift(1 + offset) > df["close"].shift(bars + offset)


# ---------------------------------------------------------------------------
# Single-candle patterns
# ---------------------------------------------------------------------------
def hammer(df: pd.DataFrame) -> pd.Series:
    """Hammer: small body at top, long lower shadow (>= 2x body), tiny upper
    shadow, after a downtrend. Bullish reversal."""
    body = _body(df)
    lower = _lower_shadow(df)
    upper = _upper_shadow(df)
    rng = _range(df)
    small_body = body <= MIN_BODY_FRACTION * rng
    long_lower = lower >= LONG_SHADOW_MULT * body
    tiny_upper = upper <= 0.25 * body
    downtrend = _prior_trend(df, bars=5, direction="down")
    return small_body & long_lower & tiny_upper & downtrend & (rng > 0)


def inverted_hammer(df: pd.DataFrame) -> pd.Series:
    """Inverted hammer: small body at bottom, long upper shadow (>= 2x body),
    tiny lower shadow, after a downtrend. Bullish reversal (needs confirmation)."""
    body = _body(df)
    upper = _upper_shadow(df)
    lower = _lower_shadow(df)
    rng = _range(df)
    small_body = body <= MIN_BODY_FRACTION * rng
    long_upper = upper >= LONG_SHADOW_MULT * body
    tiny_lower = lower <= 0.25 * body
    downtrend = _prior_trend(df, bars=5, direction="down")
    return small_body & long_upper & tiny_lower & downtrend & (rng > 0)


def hanging_man(df: pd.DataFrame) -> pd.Series:
    """Hanging man: same geometry as hammer but after an UPTREND. Bearish
    reversal (needs confirmation)."""
    body = _body(df)
    lower = _lower_shadow(df)
    upper = _upper_shadow(df)
    rng = _range(df)
    small_body = body <= MIN_BODY_FRACTION * rng
    long_lower = lower >= LONG_SHADOW_MULT * body
    tiny_upper = upper <= 0.25 * body
    uptrend = _prior_trend(df, bars=5, direction="up")
    return small_body & long_lower & tiny_upper & uptrend & (rng > 0)


def shooting_star(df: pd.DataFrame) -> pd.Series:
    """Shooting star: small body at bottom, long upper shadow (>= 2x body),
    tiny lower shadow, after an UPTREND. Bearish reversal (needs confirmation)."""
    body = _body(df)
    upper = _upper_shadow(df)
    lower = _lower_shadow(df)
    rng = _range(df)
    small_body = body <= MIN_BODY_FRACTION * rng
    long_upper = upper >= LONG_SHADOW_MULT * body
    tiny_lower = lower <= 0.25 * body
    uptrend = _prior_trend(df, bars=5, direction="up")
    return small_body & long_upper & tiny_lower & uptrend & (rng > 0)


def marubozu(df: pd.DataFrame) -> pd.Series:
    """Marubozu: no shadows (open = high or low, close = opposite extreme).
    Strong momentum. Returns True for both bullish and bearish marubozu."""
    upper = _upper_shadow(df)
    lower = _lower_shadow(df)
    rng = _range(df)
    no_shadows = (upper <= 0.05 * rng) & (lower <= 0.05 * rng)
    return no_shadows & (rng > 0)


def doji(df: pd.DataFrame) -> pd.Series:
    """Doji: open ≈ close (body <= DOJI_BODY_FRACTION of range). Indecision."""
    return _is_doji(df)


def dragonfly_doji(df: pd.DataFrame) -> pd.Series:
    """Dragonfly doji: doji with long lower shadow, tiny upper shadow."""
    return _is_doji(df) & (_lower_shadow(df) >= LONG_SHADOW_MULT * _body(df)) & \
        (_upper_shadow(df) <= 0.25 * _body(df))


def gravestone_doji(df: pd.DataFrame) -> pd.Series:
    """Gravestone doji: doji with long upper shadow, tiny lower shadow."""
    return _is_doji(df) & (_upper_shadow(df) >= LONG_SHADOW_MULT * _body(df)) & \
        (_lower_shadow(df) <= 0.25 * _body(df))


# ---------------------------------------------------------------------------
# Two-candle patterns
# ---------------------------------------------------------------------------
def bullish_engulfing(df: pd.DataFrame) -> pd.Series:
    """Bullish engulfing: small bearish candle fully engulfed by a larger
    bullish candle. After a downtrend. Bullish reversal."""
    prev_bearish = _is_bearish(df).shift(1)
    curr_bullish = _is_bullish(df)
    prev_body = _body(df).shift(1)
    curr_body = _body(df)
    # Current body engulfs prior body (opens below prior close, closes above
    # prior open).
    engulfs = (df["open"] < df["close"].shift(1)) & (df["close"] > df["open"].shift(1))
    larger = curr_body > prev_body
    downtrend = _prior_trend(df, bars=5, direction="down", offset=1)
    return prev_bearish & curr_bullish & engulfs & larger & downtrend


def bearish_engulfing(df: pd.DataFrame) -> pd.Series:
    """Bearish engulfing: small bullish candle fully engulfed by a larger
    bearish candle. After an uptrend. Bearish reversal."""
    prev_bullish = _is_bullish(df).shift(1)
    curr_bearish = _is_bearish(df)
    prev_body = _body(df).shift(1)
    curr_body = _body(df)
    engulfs = (df["open"] > df["close"].shift(1)) & (df["close"] < df["open"].shift(1))
    larger = curr_body > prev_body
    uptrend = _prior_trend(df, bars=5, direction="up", offset=1)
    return prev_bullish & curr_bearish & engulfs & larger & uptrend


def bearish_engulfing_contrarian(df: pd.DataFrame) -> pd.Series:
    """Contrarian bearish engulfing: SAME detector as ``bearish_engulfing``,
    but the assigned direction is mapped to BULLISH in ``PATTERN_DIRECTIONS``.

    Thesis (from the pooled OOS backtest): a bearish engulfing has a
    statistically significant NEGATIVE forward return in its claimed bearish
    direction (t=-2.12 OOS) — i.e., price tends to RISE after it fires. This
    synthetic pattern tests whether going LONG after a bearish engulfing
    (a liquidity grab / pullback entry, especially above the 50-SMA) beats the
    unconditional upward drift.

    Because the direction is mapped to "bullish", the confluence scorer looks
    for bullish confirmation (price > SMA50, volume surge, RSI < 40) and the
    event study compares against the bullish random-day baseline.
    """
    return bearish_engulfing(df)


def bullish_harami(df: pd.DataFrame) -> pd.Series:
    """Bullish harami: large bearish candle followed by a small bullish candle
    fully contained within the prior body. After a downtrend."""
    prev_bearish = _is_bearish(df).shift(1)
    curr_bullish = _is_bullish(df)
    prev_body = _body(df).shift(1)
    curr_body = _body(df)
    contained = (df["open"] > df["open"].shift(1)) & (df["close"] < df["close"].shift(1))
    smaller = curr_body < prev_body
    downtrend = _prior_trend(df, bars=5, direction="down", offset=1)
    return prev_bearish & curr_bullish & contained & smaller & downtrend


def bearish_harami(df: pd.DataFrame) -> pd.Series:
    """Bearish harami: large bullish candle followed by a small bearish candle
    fully contained within the prior body. After an uptrend."""
    prev_bullish = _is_bullish(df).shift(1)
    curr_bearish = _is_bearish(df)
    prev_body = _body(df).shift(1)
    curr_body = _body(df)
    contained = (df["open"] < df["open"].shift(1)) & (df["close"] > df["close"].shift(1))
    smaller = curr_body < prev_body
    uptrend = _prior_trend(df, bars=5, direction="up", offset=1)
    return prev_bullish & curr_bearish & contained & smaller & uptrend


def piercing_pattern(df: pd.DataFrame) -> pd.Series:
    """Piercing pattern: bearish candle followed by bullish candle that opens
    below the prior low and closes above the midpoint of the prior body (but
    below the prior open). After a downtrend. Bullish reversal."""
    prev_bearish = _is_bearish(df).shift(1)
    curr_bullish = _is_bullish(df)
    prev_body = _body(df).shift(1)
    prev_mid = (df["open"].shift(1) + df["close"].shift(1)) / 2
    opens_below_low = df["open"] < df["low"].shift(1)
    closes_above_mid = df["close"] > prev_mid
    closes_below_prev_open = df["close"] < df["open"].shift(1)
    downtrend = _prior_trend(df, bars=5, direction="down", offset=1)
    return prev_bearish & curr_bullish & opens_below_low & closes_above_mid & \
        closes_below_prev_open & downtrend


def dark_cloud_cover(df: pd.DataFrame) -> pd.Series:
    """Dark cloud cover: bullish candle followed by bearish candle that opens
    above the prior close and closes below the midpoint of the prior body.
    After an uptrend. Bearish reversal."""
    prev_bullish = _is_bullish(df).shift(1)
    curr_bearish = _is_bearish(df)
    prev_body = _body(df).shift(1)
    prev_mid = (df["open"].shift(1) + df["close"].shift(1)) / 2
    opens_above_close = df["open"] > df["close"].shift(1)
    closes_below_mid = df["close"] < prev_mid
    uptrend = _prior_trend(df, bars=5, direction="up", offset=1)
    return prev_bullish & curr_bearish & opens_above_close & closes_below_mid & uptrend


def kicking(df: pd.DataFrame) -> pd.Series:
    """Kicking: marubozu followed by an opposite-colored marubozu that gaps in
    the opposite direction. Strong reversal. Returns True for both directions."""
    prev_maru = marubozu(df).shift(1)
    curr_maru = marubozu(df)
    prev_bull = _is_bullish(df).shift(1)
    curr_bear = _is_bearish(df)
    prev_bear = _is_bearish(df).shift(1)
    curr_bull = _is_bullish(df)
    gap_up = df["open"] > df["high"].shift(1)
    gap_down = df["open"] < df["low"].shift(1)
    bullish_kick = prev_bear & curr_bull & gap_up & prev_maru & curr_maru
    bearish_kick = prev_bull & curr_bear & gap_down & prev_maru & curr_maru
    return bullish_kick | bearish_kick


# ---------------------------------------------------------------------------
# Three-candle patterns
# ---------------------------------------------------------------------------
def morning_star(df: pd.DataFrame) -> pd.Series:
    """Morning star: long bearish candle, small-bodied candle (gapping down),
    strong bullish candle closing above the midpoint of the first. After a
    downtrend. Bullish reversal."""
    c1_bearish = _is_bearish(df).shift(2)
    c1_long = _is_long_body(df).shift(2)
    # Star: body smaller than the prior (c1) body — the standard definition.
    c2_small = _body(df).shift(1) <= 0.5 * _body(df).shift(2)
    c3_bullish = _is_bullish(df)
    c3_long = _is_long_body(df)
    c1_mid = (df["open"].shift(2) + df["close"].shift(2)) / 2
    c3_closes_above_mid = df["close"] > c1_mid
    downtrend = _prior_trend(df, bars=5, direction="down", offset=2)
    return c1_bearish & c1_long & c2_small & c3_bullish & c3_long & \
        c3_closes_above_mid & downtrend


def evening_star(df: pd.DataFrame) -> pd.Series:
    """Evening star: long bullish candle, small-bodied candle (gapping up),
    strong bearish candle closing below the midpoint of the first. After an
    uptrend. Bearish reversal."""
    c1_bullish = _is_bullish(df).shift(2)
    c1_long = _is_long_body(df).shift(2)
    # Star: body smaller than the prior (c1) body.
    c2_small = _body(df).shift(1) <= 0.5 * _body(df).shift(2)
    c3_bearish = _is_bearish(df)
    c3_long = _is_long_body(df)
    c1_mid = (df["open"].shift(2) + df["close"].shift(2)) / 2
    c3_closes_below_mid = df["close"] < c1_mid
    uptrend = _prior_trend(df, bars=5, direction="up", offset=2)
    return c1_bullish & c1_long & c2_small & c3_bearish & c3_long & \
        c3_closes_below_mid & uptrend


def morning_doji_star(df: pd.DataFrame) -> pd.Series:
    """Morning doji star: like morning star but the middle candle is a doji."""
    c1_bearish = _is_bearish(df).shift(2)
    c1_long = _is_long_body(df).shift(2)
    c2_doji = _is_doji(df).shift(1)
    c3_bullish = _is_bullish(df)
    c3_long = _is_long_body(df)
    c1_mid = (df["open"].shift(2) + df["close"].shift(2)) / 2
    c3_closes_above_mid = df["close"] > c1_mid
    downtrend = _prior_trend(df, bars=5, direction="down", offset=2)
    return c1_bearish & c1_long & c2_doji & c3_bullish & c3_long & \
        c3_closes_above_mid & downtrend


def evening_doji_star(df: pd.DataFrame) -> pd.Series:
    """Evening doji star: like evening star but the middle candle is a doji."""
    c1_bullish = _is_bullish(df).shift(2)
    c1_long = _is_long_body(df).shift(2)
    c2_doji = _is_doji(df).shift(1)
    c3_bearish = _is_bearish(df)
    c3_long = _is_long_body(df)
    c1_mid = (df["open"].shift(2) + df["close"].shift(2)) / 2
    c3_closes_below_mid = df["close"] < c1_mid
    uptrend = _prior_trend(df, bars=5, direction="up", offset=2)
    return c1_bullish & c1_long & c2_doji & c3_bearish & c3_long & \
        c3_closes_below_mid & uptrend


def three_white_soldiers(df: pd.DataFrame) -> pd.Series:
    """Three white soldiers: three consecutive long bullish candles, each
    opening within the prior body and closing higher, near the high. After a
    downtrend. Bullish reversal/continuation."""
    c1_bull = _is_bullish(df).shift(2)
    c2_bull = _is_bullish(df).shift(1)
    c3_bull = _is_bullish(df)
    c1_long = _is_long_body(df).shift(2)
    c2_long = _is_long_body(df).shift(1)
    c3_long = _is_long_body(df)
    # Each opens within the prior body.
    c2_opens_in_c1 = (df["open"].shift(1) > df["open"].shift(2)) & \
        (df["open"].shift(1) < df["close"].shift(2))
    c3_opens_in_c2 = (df["open"] > df["open"].shift(1)) & \
        (df["open"] < df["close"].shift(1))
    # Each closes higher than the prior close.
    c2_higher = df["close"].shift(1) > df["close"].shift(2)
    c3_higher = df["close"] > df["close"].shift(1)
    # Closes near the high (small upper shadow).
    c3_near_high = _upper_shadow(df) <= 0.25 * _body(df)
    downtrend = _prior_trend(df, bars=5, direction="down", offset=2)
    return c1_bull & c2_bull & c3_bull & c1_long & c2_long & c3_long & \
        c2_opens_in_c1 & c3_opens_in_c2 & c2_higher & c3_higher & \
        c3_near_high & downtrend


def three_black_crows(df: pd.DataFrame) -> pd.Series:
    """Three black crows: three consecutive long bearish candles, each opening
    within the prior body and closing lower, near the low. After an uptrend.
    Bearish reversal/continuation."""
    c1_bear = _is_bearish(df).shift(2)
    c2_bear = _is_bearish(df).shift(1)
    c3_bear = _is_bearish(df)
    c1_long = _is_long_body(df).shift(2)
    c2_long = _is_long_body(df).shift(1)
    c3_long = _is_long_body(df)
    c2_opens_in_c1 = (df["open"].shift(1) < df["open"].shift(2)) & \
        (df["open"].shift(1) > df["close"].shift(2))
    c3_opens_in_c2 = (df["open"] < df["open"].shift(1)) & \
        (df["open"] > df["close"].shift(1))
    c2_lower = df["close"].shift(1) < df["close"].shift(2)
    c3_lower = df["close"] < df["close"].shift(1)
    c3_near_low = _lower_shadow(df) <= 0.25 * _body(df)
    uptrend = _prior_trend(df, bars=5, direction="up", offset=2)
    return c1_bear & c2_bear & c3_bear & c1_long & c2_long & c3_long & \
        c2_opens_in_c1 & c3_opens_in_c2 & c2_lower & c3_lower & \
        c3_near_low & uptrend


def abandoned_baby(df: pd.DataFrame) -> pd.Series:
    """Abandoned baby: long candle, doji gapping away, then opposite candle
    gapping back (no overlapping shadows). Rare, strong reversal. Returns True
    for both directions."""
    c1_bear = _is_bearish(df).shift(2)
    c1_bull = _is_bullish(df).shift(2)
    c1_long = _is_long_body(df).shift(2)
    c2_doji = _is_doji(df).shift(1)
    c3_bull = _is_bullish(df)
    c3_bear = _is_bearish(df)
    c3_long = _is_long_body(df)
    # Bullish: c1 bearish, doji gaps below c1 low, c3 gaps above doji high.
    gap_down_to_doji = df["high"].shift(1) < df["low"].shift(2)
    gap_up_from_doji = df["low"] > df["high"].shift(1)
    bullish = c1_bear & c1_long & c2_doji & c3_bull & c3_long & \
            gap_down_to_doji & gap_up_from_doji
    # Bearish: c1 bullish, doji gaps above c1 high, c3 gaps below doji low.
    gap_up_to_doji = df["low"].shift(1) > df["high"].shift(2)
    gap_down_from_doji = df["high"] < df["low"].shift(1)
    bearish = c1_bull & c1_long & c2_doji & c3_bear & c3_long & \
        gap_up_to_doji & gap_down_from_doji
    return bullish | bearish


def three_inside_up(df: pd.DataFrame) -> pd.Series:
    """Three inside up: harami (bearish + small bullish inside) followed by a
    bullish candle closing above the second candle's close. After a downtrend."""
    c1_bear = _is_bearish(df).shift(2)
    c2_bull = _is_bullish(df).shift(1)
    c2_inside = (df["open"].shift(1) > df["open"].shift(2)) & \
        (df["close"].shift(1) < df["close"].shift(2))
    c3_bull = _is_bullish(df)
    c3_above_c2 = df["close"] > df["close"].shift(1)
    downtrend = _prior_trend(df, bars=5, direction="down", offset=2)
    return c1_bear & c2_bull & c2_inside & c3_bull & c3_above_c2 & downtrend


def three_inside_down(df: pd.DataFrame) -> pd.Series:
    """Three inside down: harami (bullish + small bearish inside) followed by a
    bearish candle closing below the second candle's close. After an uptrend."""
    c1_bull = _is_bullish(df).shift(2)
    c2_bear = _is_bearish(df).shift(1)
    c2_inside = (df["open"].shift(1) < df["open"].shift(2)) & \
        (df["close"].shift(1) > df["close"].shift(2))
    c3_bear = _is_bearish(df)
    c3_below_c2 = df["close"] < df["close"].shift(1)
    uptrend = _prior_trend(df, bars=5, direction="up", offset=2)
    return c1_bull & c2_bear & c2_inside & c3_bear & c3_below_c2 & uptrend


def three_outside_up(df: pd.DataFrame) -> pd.Series:
    """Three outside up: bullish engulfing followed by a bullish candle closing
    above the second candle's close. After a downtrend."""
    c1_bear = _is_bearish(df).shift(2)
    c2_bull = _is_bullish(df).shift(1)
    c2_engulfs = (df["open"].shift(1) < df["close"].shift(2)) & \
        (df["close"].shift(1) > df["open"].shift(2))
    c3_bull = _is_bullish(df)
    c3_above_c2 = df["close"] > df["close"].shift(1)
    downtrend = _prior_trend(df, bars=5, direction="down", offset=2)
    return c1_bear & c2_bull & c2_engulfs & c3_bull & c3_above_c2 & downtrend


def three_outside_down(df: pd.DataFrame) -> pd.Series:
    """Three outside down: bearish engulfing followed by a bearish candle
    closing below the second candle's close. After an uptrend."""
    c1_bull = _is_bullish(df).shift(2)
    c2_bear = _is_bearish(df).shift(1)
    c2_engulfs = (df["open"].shift(1) > df["close"].shift(2)) & \
        (df["close"].shift(1) < df["open"].shift(2))
    c3_bear = _is_bearish(df)
    c3_below_c2 = df["close"] < df["close"].shift(1)
    uptrend = _prior_trend(df, bars=5, direction="up", offset=2)
    return c1_bull & c2_bear & c2_engulfs & c3_bear & c3_below_c2 & uptrend


# ---------------------------------------------------------------------------
# Multi-candle patterns
# ---------------------------------------------------------------------------
def rising_three_methods(df: pd.DataFrame) -> pd.Series:
    """Rising three methods: long bullish candle, 3 small bearish candles within
    its range, then a long bullish candle closing above the first. Continuation."""
    c1_bull = _is_bullish(df).shift(4)
    c1_long = _is_long_body(df).shift(4)
    c5_bull = _is_bullish(df)
    c5_long = _is_long_body(df)
    c5_above_c1 = df["close"] > df["close"].shift(4)
    # Middle 3 candles stay within c1's range.
    mid_high = df["high"].shift(3).max(axis=0) if False else \
        df[["high"]].shift(3).max(axis=1)
    mid_low = df[["low"]].shift(3).min(axis=1)
    within_range = (mid_high <= df["high"].shift(4)) & (mid_low >= df["low"].shift(4))
    return c1_bull & c1_long & c5_bull & c5_long & c5_above_c1 & within_range


def falling_three_methods(df: pd.DataFrame) -> pd.Series:
    """Falling three methods: long bearish candle, 3 small bullish candles within
    its range, then a long bearish candle closing below the first. Continuation."""
    c1_bear = _is_bearish(df).shift(4)
    c1_long = _is_long_body(df).shift(4)
    c5_bear = _is_bearish(df)
    c5_long = _is_long_body(df)
    c5_below_c1 = df["close"] < df["close"].shift(4)
    mid_high = df[["high"]].shift(3).max(axis=1)
    mid_low = df[["low"]].shift(3).min(axis=1)
    within_range = (mid_high <= df["high"].shift(4)) & (mid_low >= df["low"].shift(4))
    return c1_bear & c1_long & c5_bear & c5_long & c5_below_c1 & within_range


# ---------------------------------------------------------------------------
# Aggregated detection
# ---------------------------------------------------------------------------
# Canonical pattern list with direction metadata (for the backtest).
PATTERN_DIRECTIONS = {
    "hammer": "bullish",
    "inverted_hammer": "bullish",
    "hanging_man": "bearish",
    "shooting_star": "bearish",
    "marubozu": "neutral",
    "doji": "neutral",
    "dragonfly_doji": "bullish",
    "gravestone_doji": "bearish",
    "bullish_engulfing": "bullish",
    "bearish_engulfing": "bearish",
        "bearish_engulfing_contrarian": "bullish",
    "bullish_harami": "bullish",
    "bearish_harami": "bearish",
    "piercing_pattern": "bullish",
    "dark_cloud_cover": "bearish",
    "kicking": "neutral",
    "morning_star": "bullish",
    "evening_star": "bearish",
    "morning_doji_star": "bullish",
    "evening_doji_star": "bearish",
    "three_white_soldiers": "bullish",
    "three_black_crows": "bearish",
    "abandoned_baby": "neutral",
    "three_inside_up": "bullish",
    "three_inside_down": "bearish",
    "three_outside_up": "bullish",
    "three_outside_down": "bearish",
    "rising_three_methods": "bullish",
    "falling_three_methods": "bearish",
}

# The 5 core patterns from research_candlestick.md (plus their mirrors).
CORE_PATTERNS = [
    "bullish_engulfing", "bearish_engulfing",
    "bearish_engulfing_contrarian",  # contrarian test: long after bearish engulfing
    "hammer", "hanging_man",
    "morning_star", "evening_star",
    "three_white_soldiers", "three_black_crows",
    "abandoned_baby",
]

# All implemented detectors.
ALL_PATTERNS = list(PATTERN_DIRECTIONS.keys())


def detect_pattern(df: pd.DataFrame, name: str) -> pd.Series:
    """Run a single named detector. Returns a boolean Series (False where
    insufficient data)."""
    if name not in PATTERN_DIRECTIONS:
        raise ValueError(f"Unknown pattern: {name}")
    fn = globals()[name]
    result = fn(df)
    # Mask warm-up region (rolling stats not yet valid).
    result = result & (pd.Series(True, index=df.index))
    if len(df) > MIN_BARS:
        result.iloc[:MIN_BARS] = False
    return result.fillna(False).astype(bool)


def detect_all_patterns(df: pd.DataFrame, names: list[str] | None = None) -> dict[str, pd.Series]:
    """Run all (or a subset of) detectors. Returns {name: boolean Series}."""
    names = names or ALL_PATTERNS
    return {name: detect_pattern(df, name) for name in names}


def detect_core_patterns(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Run the 5 core patterns (with mirrors) from the research doc."""
    return detect_all_patterns(df, names=CORE_PATTERNS)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def _synthetic_df() -> pd.DataFrame:
    """Build a small synthetic frame with known patterns for smoke-testing."""
    # Hand-crafted OHLC rows: index 0-4 downtrend, 5 = hammer, 6-7 up,
    # 8 = bearish engulfing, 9-10 down, 11 = bullish engulfing.
    rows = [
        # open, high, low, close, volume
        (10.0, 10.4, 9.2, 9.4, 100),   # 0 bearish
        (9.4, 9.8, 8.6, 8.8, 100),     # 1 bearish
        (8.8, 9.1, 8.0, 8.2, 100),     # 2 bearish
        (8.2, 8.5, 7.4, 7.6, 100),     # 3 bearish
        (7.6, 7.9, 6.8, 7.0, 100),     # 4 bearish
        (7.0, 7.05, 5.8, 7.2, 150),    # 5 HAMMER (long lower shadow, tiny upper)
        (7.2, 7.8, 7.0, 7.7, 100),     # 6 bullish
        (7.7, 8.3, 7.5, 8.2, 100),     # 7 bullish
        (8.5, 8.6, 7.4, 7.5, 200),     # 8 BEARISH ENGULFING
        (7.5, 7.8, 7.0, 7.1, 100),     # 9 bearish
        (7.1, 7.4, 6.6, 6.7, 100),     # 10 bearish
        (6.4, 7.6, 6.3, 7.5, 200),     # 11 BULLISH ENGULFING
    ]
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


if __name__ == "__main__":
    df = _synthetic_df()
    print("Synthetic frame (12 bars):")
    print(df.to_string())
    print("\nDetected patterns (True = pattern completed at that bar):")
    sig = detect_all_patterns(df)
    for name, s in sig.items():
        hits = s[s].index.tolist()
        if hits:
            print(f"  {name:24s} -> {[str(d.date()) for d in hits]}")
    print("\nDone.")