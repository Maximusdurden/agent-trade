#!/usr/bin/env python3
"""TSLA supply/demand (support/resistance) zone detection + prev-day anchors.

Research module for a NEW sideload edge (distinct from the AMD RSI/VWAP lane and
the 3-bucket anchor rule). The premise (user, 2026-09-20):

    1. Use the PREVIOUS DAY's open and close as points we measure against.
    2. Get ALL the support and resistance zones (supply/demand zones) for TSLA.
    3. Volume is important.

This module builds the raw material for that edge:

  A. PREV-DAY ANCHORS — per trading day, the prior day's open/close/high/low and
     the prior day's RANGE (high-low) as a volatility-normalized "distance unit"
     (TSLA is high-beta, so a fixed $ distance is meaningless; a range-relative
     distance is not).

  B. S/R ZONE DETECTION (volume-weighted price clusters) — scan a trailing
     window of bars, bin prices into small buckets, SUM VOLUME per bucket, and
     find local maxima in the volume profile. Each local max is a supply/demand
     zone. Zones are classified SUPPORT (below current price) or RESISTANCE
     (above). Recent zones are weighted higher (a zone from 3 days ago matters
     more than one from 30 days ago).

  C. VOLUME CONFIRMATION — a bar's volume vs its rolling average, so the
     backtest can require "the move through a zone happened on volume."

This module is DATA + DETECTION ONLY (no trading logic). The backtest lives in
``backtest_zones_tsla.py``.

Usage:
    python -m sideload.zones_tsla --symbol TSLA --days 730
    python -m sideload.zones_tsla --symbol TSLA --days 730 --no-discord
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("ZonesTSLA")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
ET = ZoneInfo("America/New_York")

# Intraday bar interval used for zone detection + intraday simulation.
INTRADAY_INTERVAL = "5min"
# How many calendar days of intraday history to pull (paginated).
INTRADAY_DAYS_BACK = 730

# ---------------------------------------------------------------------------
# Zone-detection knobs (env-tunable so the backtest can sweep them)
# ---------------------------------------------------------------------------
# Price-bucket width as a fraction of the median price (0.005 = 0.5% buckets).
ZONE_BUCKET_PCT = float(os.getenv("ZONE_BUCKET_PCT", "0.005"))
# Trailing window (calendar days) of bars used to build the zone profile.
ZONE_WINDOW_DAYS = int(os.getenv("ZONE_WINDOW_DAYS", "30"))
# Minimum volume (as a fraction of the window's max bucket volume) for a bucket
# to count as a zone candidate. Filters out noise buckets.
ZONE_MIN_VOL_FRAC = float(os.getenv("ZONE_MIN_VOL_FRAC", "0.15"))
# Minimum separation between two distinct zones, as a fraction of price.
# Zones closer than this merge into one (prevents double-counting one level).
ZONE_MIN_SEP_PCT = float(os.getenv("ZONE_MIN_SEP_PCT", "0.01"))
# Recency half-life (days): a bar's volume weight halves every this many days,
# so recent volume counts more than old volume when building the profile.
ZONE_RECENCY_HALF_LIFE_DAYS = float(os.getenv("ZONE_RECENCY_HALF_LIFE_DAYS", "10"))


# ---------------------------------------------------------------------------
# Data loading (reuses the anchor intraday dataset helpers)
# ---------------------------------------------------------------------------
def load_daily(client: AlpacaClient, symbol: str, limit: int) -> pd.DataFrame:
    """Fetch daily bars, return a clean OHLCV frame indexed by ET date."""
    from sideload.anchor_intraday_dataset import load_daily as _load_daily
    df = _load_daily(client, symbol, limit)
    if df.empty:
        return df
    # _load_daily keeps open/high/low/close; add volume if present.
    raw = client.get_historical_bars(symbol, limit=limit, timeframe_str="day")
    if raw is not None and not raw.empty and "volume" in raw.columns:
        if isinstance(raw.index, pd.MultiIndex):
            raw = raw.reset_index(level=0, drop=True)
        raw.index = pd.to_datetime(raw.index)
        raw = raw.sort_index()
        df["volume"] = raw["volume"].reindex(df.index)
    return df


def load_intraday(client: AlpacaClient, symbol: str, days_back: int) -> pd.DataFrame:
    """Fetch intraday bars, return a frame with an ET DatetimeIndex + volume."""
    from sideload.anchor_intraday_dataset import load_intraday as _load_intraday
    df = _load_intraday(client, symbol, days_back)
    if df.empty:
        return df
    # _load_intraday keeps only 'close'; re-fetch to add volume + OHLC.
    raw = client.get_historical_bars_paginated(
        symbol, timeframe_str=INTRADAY_INTERVAL, days_back=days_back)
    if raw is None or raw.empty:
        return df
    if isinstance(raw.index, pd.MultiIndex):
        raw = raw.reset_index(level=0, drop=True)
    raw.index = pd.to_datetime(raw.index)
    if raw.index.tzinfo is None:
        raw.index = raw.index.tz_localize("UTC")
    raw.index = raw.index.tz_convert(ET)
    raw = raw.sort_index()
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in raw.columns]
    raw = raw[keep].copy()
    # Align to the same index as df (which is the close-only frame).
    df = raw.reindex(df.index)
    return df


# ---------------------------------------------------------------------------
# Prev-day anchors
# ---------------------------------------------------------------------------
def build_prev_day_frame(daily: pd.DataFrame) -> pd.DataFrame:
    """Build one row per day with prior-day anchors + prior-day range.

    Columns: date, prev_open, prev_close, prev_high, prev_low, prev_range,
    prev_range_pct (range as % of prev_close), open, close.
    """
    rows = []
    for i in range(1, len(daily)):
        po = float(daily["open"].iloc[i - 1])
        pc = float(daily["close"].iloc[i - 1])
        ph = float(daily["high"].iloc[i - 1])
        pl = float(daily["low"].iloc[i - 1])
        rng = ph - pl
        rows.append({
            "date": str(daily.index[i].date()),
            "prev_open": po,
            "prev_close": pc,
            "prev_high": ph,
            "prev_low": pl,
            "prev_range": rng,
            "prev_range_pct": rng / pc * 100.0 if pc else 0.0,
            "open": float(daily["open"].iloc[i]),
            "close": float(daily["close"].iloc[i]),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# S/R zone detection (volume-weighted price clusters)
# ---------------------------------------------------------------------------
def _recency_weight(ts: pd.Timestamp, ref: pd.Timestamp, half_life_days: float) -> float:
    """Exponential recency weight: 1.0 at ref, 0.5 after one half-life."""
    days = (ref - ts).total_seconds() / 86400.0
    if days <= 0:
        return 1.0
    return float(0.5 ** (days / half_life_days))


def detect_zones(intraday: pd.DataFrame, ref_ts: pd.Timestamp,
                 window_days: int = ZONE_WINDOW_DAYS,
                 bucket_pct: float = ZONE_BUCKET_PCT,
                 min_vol_frac: float = ZONE_MIN_VOL_FRAC,
                 min_sep_pct: float = ZONE_MIN_SEP_PCT,
                 recency_half_life_days: float = ZONE_RECENCY_HALF_LIFE_DAYS) -> list[dict]:
    """Detect supply/demand zones from volume-weighted price clusters.

    Args:
        intraday: OHLCV frame with ET DatetimeIndex.
        ref_ts: reference timestamp (the "now" we measure zones relative to).
        window_days: trailing window of bars to include.
        bucket_pct: price-bucket width as fraction of median price.
        min_vol_frac: min bucket volume (as frac of max) to be a zone candidate.
        min_sep_pct: min separation between distinct zones (frac of price).
        recency_half_life_days: recency weight half-life.

    Returns:
        List of zones, each: {price, volume, support(bool), resistance(bool),
        distance_pct (from ref price), recency_weight}.
    """
    if intraday is None or intraday.empty or "volume" not in intraday.columns:
        return []
    start = ref_ts - pd.Timedelta(days=window_days)
    win = intraday[(intraday.index >= start) & (intraday.index <= ref_ts)]
    if win.empty or "close" not in win.columns:
        return []

    prices = win["close"].to_numpy(dtype=float)
    vols = win["volume"].to_numpy(dtype=float)
    ts = win.index.to_numpy()

    # Recency weights.
    weights = np.array([
        _recency_weight(pd.Timestamp(t), ref_ts, recency_half_life_days) for t in ts
    ])
    weighted_vol = vols * weights

    # Bin prices.
    med = float(np.median(prices))
    if med <= 0:
        return []
    bin_width = med * bucket_pct
    bins = np.arange(prices.min() - bin_width, prices.max() + bin_width, bin_width)
    idx = np.digitize(prices, bins)
    # Aggregate weighted volume per bin.
    df = pd.DataFrame({"bin": idx, "wvol": weighted_vol})
    prof = df.groupby("bin")["wvol"].sum()
    if prof.empty:
        return []
    bin_center = bins[:-1] + bin_width / 2.0
    prof = prof.reindex(range(len(bin_center)), fill_value=0.0)
    prof_arr = prof.to_numpy(dtype=float)

    max_vol = float(prof_arr.max())
    if max_vol <= 0:
        return []
    # Candidate buckets: above min_vol_frac of max, and a local max.
    cand = []
    for b in range(1, len(prof_arr) - 1):
        if prof_arr[b] < min_vol_frac * max_vol:
            continue
        if prof_arr[b] >= prof_arr[b - 1] and prof_arr[b] >= prof_arr[b + 1]:
            cand.append((bin_center[b], prof_arr[b]))
    if not cand:
        return []

    # Merge candidates closer than min_sep_pct (keep the higher-volume one).
    cand.sort(key=lambda x: x[1], reverse=True)
    zones = []
    for price, vol in cand:
        if all(abs(price - z["price"]) / price >= min_sep_pct for z in zones):
            zones.append({"price": float(price), "volume": float(vol)})
    zones.sort(key=lambda z: z["price"])

    ref_price = float(win["close"].iloc[-1])
    for z in zones:
        z["support"] = z["price"] < ref_price
        z["resistance"] = z["price"] > ref_price
        z["distance_pct"] = (z["price"] - ref_price) / ref_price * 100.0
    return zones


def nearest_zone(zones: list[dict], price: float, side: str) -> dict | None:
    """Return the nearest zone on a side ('above'/'below') of ``price``."""
    if side == "above":
        above = [z for z in zones if z["price"] > price]
        if not above:
            return None
        return min(above, key=lambda z: z["price"] - price)
    below = [z for z in zones if z["price"] < price]
    if not below:
        return None
    return max(below, key=lambda z: price - z["price"])


# ---------------------------------------------------------------------------
# Volume confirmation
# ---------------------------------------------------------------------------
def add_volume_confirmation(intraday: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """Add rolling-average volume ratio: vol / rolling_mean(vol).

    A value > 1.0 means the bar traded above its recent average volume — the
    "volume confirms the move" filter.
    """
    df = intraday.copy()
    if "volume" not in df.columns:
        df["vol_ratio"] = 1.0
        return df
    roll = df["volume"].rolling(window, min_periods=1).mean()
    df["vol_ratio"] = df["volume"] / roll.replace(0, np.nan)
    df["vol_ratio"] = df["vol_ratio"].fillna(1.0)
    return df


# ---------------------------------------------------------------------------
# Dataset builder (one row per day with zones + prev-day anchors)
# ---------------------------------------------------------------------------
def build_zone_dataset(client: AlpacaClient, symbol: str, days_back: int) -> pd.DataFrame:
    """Build a per-day dataset: prev-day anchors + detected zones at 10:00 ET.

    For each trading day, we detect zones using the trailing window UP TO 10:00
    ET that day (so the backtest can decide an entry at 10:00 using only
    information available at 10:00 — no lookahead). We also record the nearest
    support/resistance zone and their distances from the 10:00 price.
    """
    daily = load_daily(client, symbol, limit=days_back)
    if len(daily) < 30:
        logger.warning(f"Not enough daily bars for {symbol} ({len(daily)}).")
        return pd.DataFrame()
    intraday = load_intraday(client, symbol, days_back)
    if intraday.empty:
        logger.warning(f"No intraday bars for {symbol}.")
        return pd.DataFrame()
    intraday = add_volume_confirmation(intraday)

    prev = build_prev_day_frame(daily)
    rows = []
    for _, row in prev.iterrows():
        day = pd.Timestamp(row["date"])
        day_et = day.tz_localize(ET)
        ref_ts = day_et.replace(hour=10, minute=0, second=0, microsecond=0)
        # Only use bars up to 10:00 (no lookahead).
        zones = detect_zones(intraday, ref_ts)
        # 10:00 price from intraday.
        window = intraday[(intraday.index >= ref_ts - pd.Timedelta(hours=1)) &
                          (intraday.index <= ref_ts)]
        if window.empty:
            continue
        p10 = float(window["close"].iloc[-1])
        vol10 = float(window["volume"].iloc[-1]) if "volume" in window.columns else 0.0
        vol_ratio10 = float(window["vol_ratio"].iloc[-1]) if "vol_ratio" in window.columns else 1.0

        sup = nearest_zone(zones, p10, "below")
        res = nearest_zone(zones, p10, "above")
        rows.append({
            "date": row["date"],
            "prev_open": row["prev_open"],
            "prev_close": row["prev_close"],
            "prev_high": row["prev_high"],
            "prev_low": row["prev_low"],
            "prev_range": row["prev_range"],
            "prev_range_pct": row["prev_range_pct"],
            "open": row["open"],
            "p10": p10,
            "close": row["close"],
            "vol_ratio_10": vol_ratio10,
            "n_zones": len(zones),
            "nearest_support": sup["price"] if sup else None,
            "nearest_resistance": res["price"] if res else None,
            "dist_to_support_pct": sup["distance_pct"] if sup else None,
            "dist_to_resistance_pct": res["distance_pct"] if res else None,
            "support_vol": sup["volume"] if sup else None,
            "resistance_vol": res["volume"] if res else None,
        })
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="TSLA S/R zone + prev-day anchor dataset")
    parser.add_argument("--symbol", default="TSLA")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-zones")
    client = AlpacaClient()

    try:
        df = build_zone_dataset(client, args.symbol.upper(), args.days)
        if df.empty:
            logger.error("No dataset produced.")
            return
        out_path = os.path.join(OUT_DIR, f"zones_{args.symbol.lower()}.csv")
        df.to_csv(out_path, index=False)
        logger.info(f"Wrote {out_path} ({len(df)} rows)")

        print(f"\n=== {args.symbol.upper()} ZONE DATASET ({len(df)} days) ===")
        print(f"Columns: {list(df.columns)}")
        print(f"\nZone stats:")
        print(f"  mean zones/day: {df['n_zones'].mean():.1f}")
        print(f"  days with support: {df['nearest_support'].notna().sum()}")
        print(f"  days with resistance: {df['nearest_resistance'].notna().sum()}")
        print(f"  mean dist to support: {df['dist_to_support_pct'].mean():.2f}%")
        print(f"  mean dist to resistance: {df['dist_to_resistance_pct'].mean():.2f}%")
        print(f"\nSample rows:")
        print(df.head(10).to_string())
        if not args.no_discord:
            try:
                send_discord_message(
                    f"{args.symbol.upper()} zone dataset built: {len(df)} rows -> {out_path}"
                )
            except Exception as e:
                logger.warning(f"Discord notify failed: {e}")
    except Exception as e:
        logger.critical(f"Zone dataset build failed: {e}")
        log_exception_to_jira(e, "TSLA Zone Dataset Failure")
        raise


if __name__ == "__main__":
    main()