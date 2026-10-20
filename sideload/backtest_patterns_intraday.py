#!/usr/bin/env python3
"""Intraday (1-hour bar) candlestick pattern confluence backtest.

Adapts the daily ``backtest_patterns`` pipeline to 1-hour bars to test whether
microstructure-level candlestick patterns yield an exploitable edge beyond
baseline drift.

Intraday-specific handling:
  1. Overnight gap distortion — ``session_aware=True`` masks forward windows
     that cross a session boundary (a signal near the close is NOT evaluated
     on an unexecutable overnight hold).
  2. Transaction friction — same 5 bps round-trip cost; on 1h horizons this
     is a much larger fraction of the forward move, so weak signals die fast.
  3. Data granularity — pulls 1h bars via yfinance, filters strictly to
     regular market hours (09:30-16:00 ET), ~2y lookback (2024-2026).

Horizons are in BARS: [1, 2, 4, 8] = ~1h, 2h, half-day, full-day holds.
NOTE: with session-aware masking (no overnight holds), h=8 has ~0 valid bars
because an 8-hour hold always spans the session close — the effective max
intraday horizon is ~6 bars (6.5h session).

OOS split: Train 2024-01-01 -> 2025-06-30; Test 2025-07-01 -> 2026-10-01.

Usage:
    python -m sideload.backtest_patterns_intraday
    python -m sideload.backtest_patterns_intraday --symbol SPY --symbol NVDA
    python -m sideload.backtest_patterns_intraday --quick   # 2025+ only
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime

import pandas as pd
import numpy as np

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Reuse the shared academic machinery from the daily backtest.
from sideload.backtest_patterns import (
    DEFAULT_UNIVERSE,
    COST_BPS,
    BONFERRONI_T,
    MIN_BARS,
    CONFLUENCE_WEIGHTS,
    MAX_CONF_SCORE,
    VOLUME_SURGE_MULT,
    RSI_OVERSOLD,
    RSI_OVERBOUGHT,
    add_confluence_indicators,
    confluence_score,
    forward_returns,
    hac_tstat,
    random_day_baseline,
    pool_baselines,
    pattern_event_study,
    pool_results,
    _strip_events,
    format_results,
)
from sideload.patterns import (
    detect_all_patterns,
    PATTERN_DIRECTIONS,
    CORE_PATTERNS,
)
from sideload.jira_logging import setup_jira_logging, log_exception_to_jira

logger = logging.getLogger("BacktestPatternsIntraday")

# ---------------------------------------------------------------------------
# Intraday constants
# ---------------------------------------------------------------------------
# Forward horizons in BARS (1h bars): 1h, 2h, half-day, full-day holds.
HORIZONS = [1, 2, 4, 8]

# Regular market hours (ET) — filter bars strictly to these.
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"

# OOS split: Train 2024-10 -> 2025-09; Test 2025-10 -> 2026.
OOS_SPLIT_DATE = "2025-10-01"
TRAIN_LABEL = "2024H2-2025H1"
TEST_LABEL = "2025H2-2026"

# Default lookback (~2 years) to manage intraday download limits. yfinance
# caps 1h data at the last 730 days, so the default start is ~730 days back.
DEFAULT_START = "2024-10-13"
DEFAULT_END = "2026-10-01"


# ---------------------------------------------------------------------------
# Intraday data ingestion
# ---------------------------------------------------------------------------
def load_intraday_bars(symbol: str, start: str = DEFAULT_START,
                       end: str = DEFAULT_END,
                       interval: str = "1h") -> pd.DataFrame:
    """Fetch 1-hour OHLCV bars via yfinance and filter to regular market hours.

    Returns a DataFrame with columns: open, high, low, close, volume, indexed
    by the bar timestamp (ET). Bars outside 09:30-16:00 ET are dropped to avoid
    extended-hours illiquidity distortions.

    yfinance caps intraday (1h) history at the last 730 days; the start date is
    clamped to that window so the request never fails.
    """
    try:
        import yfinance as yf
        # Clamp start to the 730-day intraday window.
        from datetime import datetime, timedelta
        max_lookback = datetime.now() - timedelta(days=730)
        start_dt = datetime.strptime(start, "%Y-%m-%d")
        if start_dt < max_lookback:
            start = max_lookback.strftime("%Y-%m-%d")
            logger.info(f"  Clamped start to {start} (730-day intraday limit)")
        df = yf.download(symbol, start=start, end=end, interval=interval,
                         auto_adjust=True, progress=False)
        if df is None or df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume",
        })
        df = df[["open", "high", "low", "close", "volume"]].dropna()

        # Filter to regular market hours (09:30-16:00 ET). yfinance returns
        # tz-aware timestamps; normalize to ET for the hour check.
        if df.index.tz is None:
            df.index = df.index.tz_localize("America/New_York")
        else:
            df.index = df.index.tz_convert("America/New_York")
        hour_min = df.index.hour * 60 + df.index.minute
        open_min = int(MARKET_OPEN.split(":")[0]) * 60 + int(MARKET_OPEN.split(":")[1])
        close_min = int(MARKET_CLOSE.split(":")[0]) * 60 + int(MARKET_CLOSE.split(":")[1])
        df = df[(hour_min >= open_min) & (hour_min < close_min)]
        return df
    except Exception as e:
        logger.warning(f"yfinance 1h failed for {symbol}: {e}")
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_symbol(symbol: str, start: str, end: str) -> dict:
    """Run the full intraday event study for one symbol."""
    logger.info(f"Loading {symbol} 1h bars ({start} to {end})...")
    df = load_intraday_bars(symbol, start=start, end=end)
    if df.empty or len(df) < 200:
        logger.warning(f"Not enough data for {symbol}: {len(df)} bars")
        return {"symbol": symbol, "error": "insufficient data"}

    df = add_confluence_indicators(df)
    logger.info(f"  {len(df)} bars, {df.index[0]} to {df.index[-1]}")

    # Detect patterns.
    signals = detect_all_patterns(df, names=CORE_PATTERNS)
    n_signals = {name: int(s.sum()) for name, s in signals.items()}
    logger.info(f"  Signals: {n_signals}")

    # Random-bar baseline (bullish direction; bearish mirrors it).
    baseline = random_day_baseline(df, HORIZONS, "bullish")

    # Event study — session-aware so signals near the close are not evaluated
    # on unexecutable overnight holds.
    results = pattern_event_study(df, signals, HORIZONS, symbol=symbol,
                                  session_aware=True)

    # OOS split: train < 2025-07-01, test >= 2025-07-01.
    split_date = pd.Timestamp(OOS_SPLIT_DATE, tz=df.index.tz)
    df_train = df[df.index < split_date]
    df_test = df[df.index >= split_date]
    signals_train = {k: v[df.index < split_date] for k, v in signals.items()}
    signals_test = {k: v[df.index >= split_date] for k, v in signals.items()}
    results_train = pattern_event_study(df_train, signals_train, HORIZONS,
                                        symbol=symbol, session_aware=True)
    results_test = pattern_event_study(df_test, signals_test, HORIZONS,
                                       symbol=symbol, session_aware=True)

    return {
        "symbol": symbol,
        "bars": len(df),
        "start": str(df.index[0]),
        "end": str(df.index[-1]),
        "n_signals": n_signals,
        "baseline": baseline,
        "results": results,
        "results_train": results_train,
        "results_test": results_test,
        "markdown": format_results(results, baseline, symbol,
                                   results_train, results_test,
                                   h_suffix="h",
                                   train_label=TRAIN_LABEL,
                                   test_label=TEST_LABEL),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Intraday (1h) candlestick pattern backtest")
    parser.add_argument("--symbol", action="append", default=None,
                        help="Symbol(s) to test (repeatable); default = broad universe")
    parser.add_argument("--start", default=DEFAULT_START, help="Start date")
    parser.add_argument("--end", default=DEFAULT_END, help="End date")
    parser.add_argument("--quick", action="store_true",
                        help="Quick mode: fewer bars (2025+)")
    parser.add_argument("--out", default="sideload/backtest_patterns_intraday.json",
                        help="Output JSON path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-patterns-intraday")

    universe = args.symbol or DEFAULT_UNIVERSE
    if args.quick:
        args.start = "2025-01-01"

    all_results = {}
    per_symbol_results = []
    per_symbol_train = []
    per_symbol_test = []
    per_symbol_baselines = []

    try:
        for symbol in universe:
            logger.info(f"=== {symbol} ===")
            res = run_symbol(symbol, args.start, args.end)
            if "error" in res:
                logger.warning(f"Skipping {symbol}: {res['error']}")
                continue
            all_results[symbol] = res
            per_symbol_results.append(res["results"])
            per_symbol_train.append(res["results_train"])
            per_symbol_test.append(res["results_test"])
            per_symbol_baselines.append(res["baseline"])
            # Per-symbol markdown report.
            md_path = f"sideload/backtest_patterns_intraday_{symbol}.md"
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(res["markdown"])
            logger.info(f"  Wrote {md_path}")

        # Pooled universe report.
        if per_symbol_results:
            pooled_results = pool_results(per_symbol_results, horizons=HORIZONS)
            pooled_train = pool_results(per_symbol_train, horizons=HORIZONS)
            pooled_test = pool_results(per_symbol_test, horizons=HORIZONS)
            pooled_baseline = pool_baselines(per_symbol_baselines, horizons=HORIZONS)
            universe_label = "+".join(all_results.keys())
            pooled_md = format_results(pooled_results, pooled_baseline,
                                       universe_label,
                                       pooled_train, pooled_test,
                                       pooled=True,
                                       h_suffix="h",
                                       train_label=TRAIN_LABEL,
                                       test_label=TEST_LABEL)
            print("\n" + pooled_md + "\n")
            with open("sideload/backtest_patterns_intraday_POOLED.md", "w",
                      encoding="utf-8") as f:
                f.write(pooled_md)
            logger.info("  Wrote sideload/backtest_patterns_intraday_POOLED.md")

            # Attach pooled (events stripped) to the JSON dump.
            all_results["__POOLED__"] = {
                "universe": list(all_results.keys()),
                "baseline": {str(k): {kk: vv for kk, vv in v.items()
                                      if kk != "samples"}
                             for k, v in pooled_baseline.items()},
                "results": _strip_events(pooled_results),
                "results_train": _strip_events(pooled_train),
                "results_test": _strip_events(pooled_test),
                "markdown": pooled_md,
            }

        # Strip per-symbol events + baseline samples for the JSON dump.
        for sym, res in all_results.items():
            if sym == "__POOLED__":
                continue
            res["baseline"] = {str(k): {kk: vv for kk, vv in v.items()
                                        if kk != "samples"}
                               for k, v in res["baseline"].items()}
            res["results"] = _strip_events(res["results"])
            res["results_train"] = _strip_events(res["results_train"])
            res["results_test"] = _strip_events(res["results_test"])

        with open(args.out, "w") as f:
            json.dump(all_results, f, indent=2, default=str)
        logger.info(f"Results saved to {args.out}")
    except Exception as e:
        logger.critical(f"Intraday backtest failed: {e}")
        log_exception_to_jira(e, "Intraday Pattern Backtest Failure")
        raise


if __name__ == "__main__":
    main()