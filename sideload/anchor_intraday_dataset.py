#!/usr/bin/env python3
"""SPY intraday anchor dataset — one row per trading day.

Pivots the prior-day OHLC anchor study into a per-DAY dataset that pairs the
previous day's HIGH/CLOSE with the current day's OPEN and the price at 10:00 ET.
This is the raw material for the intraday "get in and get out early" strategy:
we want to see how the 10:00 price relates to the prior day's levels.

Columns:
    date, ticker, previous_day_high, previous_day_close,
    current_date_open, price_at_10:00

Data:
  - previous_day_high / previous_day_close: from DAILY bars (prior trading day).
  - current_date_open: today's daily-bar OPEN.
  - price_at_10:00: the 5-min bar CLOSE nearest to 10:00 ET on the current day.

Usage:
    python -m sideload.anchor_intraday_dataset --symbol SPY --days 730
    python -m sideload.anchor_intraday_dataset --symbol SPY --days 730 --no-discord
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("AnchorIntradayDataset")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
ET = ZoneInfo("America/New_York")

# The intraday bar interval used to sample the 10:00 price.
INTRADAY_INTERVAL = "5min"
# Target time (ET) for the intraday sample.
TARGET_TIME = dtime(10, 0)


def _to_et(ts) -> pd.Timestamp:
    """Convert a UTC timestamp to US/Eastern."""
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(ET)


def load_daily(client: AlpacaClient, symbol: str, limit: int) -> pd.DataFrame:
    """Fetch daily bars, return a clean OHLC frame indexed by ET date."""
    df = client.get_historical_bars(symbol, limit=limit, timeframe_str="day")
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index(level=0, drop=True)
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    keep = [c for c in ("open", "high", "low", "close") if c in df.columns]
    df = df[keep].copy()
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


def load_intraday(client: AlpacaClient, symbol: str, days_back: int) -> pd.DataFrame:
    """Fetch intraday bars, return a frame with an ET DatetimeIndex."""
    df = client.get_historical_bars_paginated(
        symbol, timeframe_str=INTRADAY_INTERVAL, days_back=days_back)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index(level=0, drop=True)
    df.index = pd.to_datetime(df.index)
    # Convert to US/Eastern so all times are NY time.
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ET)
    df = df.sort_index()
    if "close" not in df.columns:
        return pd.DataFrame()
    return df[["close"]].copy()


def _price_at_10am(intraday: pd.DataFrame, day: pd.Timestamp) -> float | None:
    """Return the intraday close nearest to 10:00 ET on ``day`` (ET date)."""
    day_et = day.tz_convert(ET) if day.tzinfo is not None else day.tz_localize(ET)
    start = day_et.replace(hour=9, minute=0, second=0, microsecond=0)
    end = day_et.replace(hour=11, minute=0, second=0, microsecond=0)
    window = intraday[(intraday.index >= start) & (intraday.index <= end)]
    if window.empty:
        return None
    # Bar closest to 10:00 ET.
    target = day_et.replace(hour=10, minute=0, second=0, microsecond=0)
    deltas = (window.index - target).to_numpy()
    idx = int(np.abs(deltas).argmin())
    return float(window["close"].iloc[idx])


def build_dataset(client: AlpacaClient, symbol: str, days_back: int) -> pd.DataFrame:
    """Build the pivoted per-day dataset for a symbol."""
    daily = load_daily(client, symbol, limit=days_back)
    if len(daily) < 30:
        logger.warning(f"Not enough daily bars for {symbol} ({len(daily)}).")
        return pd.DataFrame()

    intraday = load_intraday(client, symbol, days_back)
    if intraday.empty:
        logger.warning(f"No intraday bars for {symbol}.")
        return pd.DataFrame()

    # Convert daily index to ET dates for clean day grouping.
    daily_et = daily.copy()
    daily_et.index = daily_et.index.tz_localize("UTC").tz_convert(ET) \
        if daily_et.index.tzinfo is None else daily_et.index.tz_convert(ET)
    daily_et["et_date"] = daily_et.index.date

    rows = []
    dates = daily_et["et_date"].tolist()
    for i in range(1, len(dates)):
        cur_date = dates[i]
        prev_date = dates[i - 1]
        prev_high = float(daily_et["high"].iloc[i - 1])
        prev_close = float(daily_et["close"].iloc[i - 1])
        cur_open = float(daily_et["open"].iloc[i])
        # 10:00 ET price on the current day.
        cur_ts = daily_et.index[i]
        p10 = _price_at_10am(intraday, cur_ts)
        rows.append({
            "date": str(cur_date),
            "ticker": symbol,
            "previous_day_high": prev_high,
            "previous_day_close": prev_close,
            "current_date_open": cur_open,
            "price_at_10:00": p10,
            # Entry-condition flags (1 = condition true).
            "open_gt_prev_high": int(cur_open > prev_high),
            "open_gt_prev_close": int(cur_open > prev_close),
            "p10_gt_prev_high": int(p10 > prev_high) if p10 is not None else None,
            "p10_gt_prev_close": int(p10 > prev_close) if p10 is not None else None,
        })

    out = pd.DataFrame(rows)
    out = out.dropna(subset=["price_at_10:00"])
    return out


def summarize_rates(df: pd.DataFrame) -> dict:
    """Aggregate the entry-condition flags into rates."""
    cols = ["open_gt_prev_high", "open_gt_prev_close",
            "p10_gt_prev_high", "p10_gt_prev_close"]
    out = {"n_days": int(len(df))}
    for c in cols:
        vals = df[c].dropna().astype(int)
        n = int(len(vals))
        out[c] = {
            "n": n,
            "count_true": int(vals.sum()),
            "rate": float(vals.mean()) if n else float("nan"),
        }
    return out


def print_rates(symbol: str, rates: dict) -> None:
    print(f"\n{'=' * 72}")
    print(f"ENTRY-CONDITION RATES — {symbol}  ({rates['n_days']} days)")
    print(f"{'=' * 72}")
    print(f"{'condition':<28}{'true':>6}{'total':>8}{'rate':>9}")
    print("-" * 51)
    labels = {
        "open_gt_prev_high": "open > prev high",
        "open_gt_prev_close": "open > prev close",
        "p10_gt_prev_high": "10:00 > prev high",
        "p10_gt_prev_close": "10:00 > prev close",
    }
    for c, label in labels.items():
        s = rates[c]
        print(f"{label:<28}{s['count_true']:>6}{s['n']:>8}{s['rate']:>8.1%}")
    print("-" * 51)


def _notify_discord(symbol: str, n: int, path: str) -> None:
    try:
        send_discord_message(
            f"SPY intraday anchor dataset built: {n} rows for {symbol} -> {path}"
        )
    except Exception as e:
        logger.warning(f"Discord notify failed: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SPY intraday anchor dataset")
    parser.add_argument("--symbol", default="SPY", help="Ticker (default SPY)")
    parser.add_argument("--days", type=int, default=730,
                        help="Calendar days of history (~2yrs)")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord notification")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-anchor-intraday")
    client = AlpacaClient()

    try:
        symbol = args.symbol.upper()
        df = build_dataset(client, symbol, args.days)
        if df.empty:
            logger.error("No rows produced.")
            return
        path = os.path.join(OUT_DIR, f"anchor_intraday_{symbol.lower()}.csv")
        df.to_csv(path, index=False)
        logger.info(f"Wrote {path} ({len(df)} rows)")
        print(df.head(15).to_string(index=False))
        rates = summarize_rates(df)
        print_rates(symbol, rates)
        if not args.no_discord:
            _notify_discord(symbol, len(df), path)
    except Exception as e:
        logger.critical(f"Intraday dataset build failed: {e}")
        log_exception_to_jira(e, "Anchor Intraday Dataset Failure")
        raise


if __name__ == "__main__":
    main()