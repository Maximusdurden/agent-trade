#!/usr/bin/env python3
"""Candlestick pattern confluence backtest for the sideload lane.

Tests whether candlestick patterns (from ``patterns.py``) add value as a
FILTER/CONFLUENCE layer across a multi-symbol universe, using the
academic-grade methodology from ``research_candlestick.md``:

  1. Random-day baseline control — a pattern adds real signal only if its
     forward return beats a random draw of days with the same directional
     assignment.
  2. Net of costs — charge spread + slippage on every round trip (5 bps).
  3. Multiple-comparisons discipline — Bonferroni threshold on t-stats.
  4. HAC / Newey-West t-stats — overlapping forward windows.
  5. Out-of-sample split — train 2010-2020, test 2021-2026, reported in
     markdown alongside full-sample results.

Multi-symbol aggregation:
  - Runs the event study per symbol, then POOLS cross-sectional pattern
    occurrences (combined n + pooled expectancy) so rare patterns
    (abandoned_baby, three_white_soldiers) accumulate enough samples.

Confluence scoring (weighted, prevents signal starvation):
  - trend alignment (SMA-50) = 2 points, volume = 1 point, RSI = 1 point.
  - Max score 4. No hard gate — the report shows the full score distribution
    and the conf>=1 / conf>=2 buckets so the user can pick a threshold.

Usage:
    python -m sideload.backtest_patterns                      # default universe
    python -m sideload.backtest_patterns --symbol SPY --symbol NVDA
    python -m sideload.backtest_patterns --quick              # 2020+ only
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

from sideload.patterns import (
    detect_all_patterns,
    PATTERN_DIRECTIONS,
    CORE_PATTERNS,
)
from sideload.jira_logging import setup_jira_logging, log_exception_to_jira

logger = logging.getLogger("BacktestPatterns")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Default multi-symbol universe (broad equity basket: index ETFs + liquid
# large-caps). Override with --symbol.
DEFAULT_UNIVERSE = ["SPY", "QQQ", "IWM", "NVDA", "AMD", "AAPL", "MSFT"]

# Cost model: round-trip cost as fraction of price (spread + slippage + fees).
# ~2 bps for SPY, ~5 bps for NVDA. Conservative default: 5 bps round trip.
COST_BPS = 5.0 / 10000.0

# Forward return horizons (trading days).
HORIZONS = [1, 3, 5, 10]

# Bonferroni threshold: 9 core patterns x 2 directions x 4 horizons = 72 tests.
# |t| >= 2.64 for alpha=0.05 (approx, using 72 tests).
BONFERRONI_T = 2.64

# Minimum bars for a valid signal (warm-up for rolling stats).
MIN_BARS = 30

# Out-of-sample split date (train < split, test >= split).
OOS_SPLIT_DATE = "2021-01-01"

# Weighted confluence scoring: trend alignment matters most (2x), volume and
# RSI each 1x. Max score = 4.0. This prevents signal starvation vs the old
# rigid score>=2 gate while still rewarding multi-layer confirmation.
CONFLUENCE_WEIGHTS = {"trend": 2.0, "volume": 1.0, "rsi": 1.0}
MAX_CONF_SCORE = 4.0

# Volume surge threshold (x average) for the volume confluence layer.
VOLUME_SURGE_MULT = 1.5

# RSI thresholds for the oscillator confluence layer.
RSI_OVERSOLD = 40.0
RSI_OVERBOUGHT = 60.0


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_daily_bars(symbol: str, start: str = "2010-01-01",
                    end: str = "2026-10-01") -> pd.DataFrame:
    """Fetch daily OHLCV for a symbol via yfinance (fallback) or Alpaca.

    Returns a DataFrame with columns: open, high, low, close, volume.
    """
    try:
        import yfinance as yf
        df = yf.download(symbol, start=start, end=end, auto_adjust=True, progress=False)
        if df is None or df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume",
        })
        df = df[["open", "high", "low", "close", "volume"]].dropna()
        return df
    except Exception as e:
        logger.warning(f"yfinance failed for {symbol}: {e}")
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Confluence indicators
# ---------------------------------------------------------------------------
def add_confluence_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add SMA-50, volume average, and RSI-14 columns for confluence scoring."""
    df = df.copy()
    df["sma50"] = df["close"].rolling(50).mean()
    df["vol_avg20"] = df["volume"].rolling(20).mean()
    # RSI-14 (Wilder's smoothing).
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))
    df["rsi14"] = df["rsi14"].fillna(50.0)
    return df


def confluence_score(df: pd.DataFrame, i: int, direction: str) -> float:
    """Weighted confluence score (0.0-4.0) confirming the pattern direction.

    Layers (weighted so trend alignment matters most):
      - trend (SMA-50 alignment): 2 points
      - volume (surge > 1.5x 20-bar avg): 1 point
      - RSI (oversold < 40 / overbought > 60): 1 point

    direction="bullish": trend up, volume high, RSI oversold.
    direction="bearish": trend down, volume high, RSI overbought.
    """
    score = 0.0
    try:
        close = float(df["close"].iloc[i])
        sma50 = float(df["sma50"].iloc[i])
        vol = float(df["volume"].iloc[i])
        vol_avg = float(df["vol_avg20"].iloc[i])
        rsi = float(df["rsi14"].iloc[i])
    except (KeyError, IndexError, ValueError):
        return 0.0

    if direction == "bullish":
        if not np.isnan(sma50) and close > sma50:
            score += CONFLUENCE_WEIGHTS["trend"]
        if not np.isnan(vol_avg) and vol_avg > 0 and vol > VOLUME_SURGE_MULT * vol_avg:
            score += CONFLUENCE_WEIGHTS["volume"]
        if not np.isnan(rsi) and rsi < RSI_OVERSOLD:
            score += CONFLUENCE_WEIGHTS["rsi"]
    else:  # bearish
        if not np.isnan(sma50) and close < sma50:
            score += CONFLUENCE_WEIGHTS["trend"]
        if not np.isnan(vol_avg) and vol_avg > 0 and vol > VOLUME_SURGE_MULT * vol_avg:
            score += CONFLUENCE_WEIGHTS["volume"]
        if not np.isnan(rsi) and rsi > RSI_OVERBOUGHT:
            score += CONFLUENCE_WEIGHTS["rsi"]
    return score


# ---------------------------------------------------------------------------
# Forward returns
# ---------------------------------------------------------------------------
def _session_boundary_mask(df: pd.DataFrame, horizons: list[int]) -> dict[int, np.ndarray]:
    """For each horizon h, a boolean mask over bars whose forward window
    [i, i+h] crosses a session boundary (the last bar of the day is within
    (i, i+h]).

    Used by intraday studies to avoid unexecutable overnight holds: a bar i is
    masked for horizon h if there exists a last-bar-of-day index li with
    i < li <= i+h — i.e., the h-bar forward return would span the close of one
    session and the open of the next (an overnight gap).
    """
    dates = pd.Series(df.index.date, index=df.index)
    is_last_of_day = dates != dates.shift(-1)
    last_idx = np.where(is_last_of_day.to_numpy())[0]
    n = len(df)
    out = {}
    for h in horizons:
        mask = np.zeros(n, dtype=bool)
        for li in last_idx:
            lo = max(0, li - h)
            mask[lo:li] = True
        out[h] = mask
    return out


def forward_returns(df: pd.DataFrame, horizons: list[int],
                    session_aware: bool = False) -> dict[int, np.ndarray]:
    """Compute forward returns (close-to-close) for each horizon.

    Returns {horizon: np.ndarray} aligned to df.index. NaN where insufficient
    future data.

    ``session_aware=True`` additionally masks bars whose forward window would
    cross a session boundary (overnight hold) — used for intraday bars so
    signals near the close are not evaluated on unexecutable overnight gaps.
    """
    close = df["close"].to_numpy(dtype=float)
    n = len(close)
    boundary_mask = _session_boundary_mask(df, horizons) if session_aware else None
    out = {}
    for h in horizons:
        fwd = np.full(n, np.nan)
        if h < n:
            fwd[: n - h] = close[h:] / close[: n - h] - 1.0
        if boundary_mask is not None:
            fwd[boundary_mask[h]] = np.nan
        out[h] = fwd
    return out


# ---------------------------------------------------------------------------
# HAC (Newey-West) t-stat
# ---------------------------------------------------------------------------
def hac_tstat(x: np.ndarray, max_lag: int | None = None) -> float:
    """Newey-West HAC t-stat for the mean of x (vs 0)."""
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 5:
        return 0.0
    mean = x.mean()
    if mean == 0:
        return 0.0
    resid = x - mean
    var = np.mean(resid ** 2)
    if max_lag is None:
        max_lag = int(np.floor(4 * (n / 100) ** (2 / 9)))
    max_lag = max(0, min(max_lag, n - 2))
    for lag in range(1, max_lag + 1):
        cov = np.mean(resid[lag:] * resid[:-lag])
        var += 2 * (1 - lag / (max_lag + 1)) * cov
    if var <= 0:
        return 0.0
    se = np.sqrt(var / n)
    return mean / se if se > 0 else 0.0


# ---------------------------------------------------------------------------
# Random-day baseline
# ---------------------------------------------------------------------------
def random_day_baseline(df: pd.DataFrame, horizons: list[int],
                        direction: str, n_draws: int = 5000,
                        seed: int = 42) -> dict[int, dict]:
    """Random-day baseline: mean forward return of random days with the same
    directional assignment (long for bullish, short for bearish).

    Returns {horizon: {mean, tstat, n, samples}}. ``samples`` is the raw draw
    array so the pooled-universe baseline can concatenate across symbols.
    """
    rng = np.random.default_rng(seed)
    fwd = forward_returns(df, horizons)
    out = {}
    for h in horizons:
        f = fwd[h]
        valid = ~np.isnan(f)
        idx = np.where(valid)[0]
        if len(idx) == 0:
            out[h] = {"mean": 0.0, "tstat": 0.0, "n": 0, "samples": np.array([])}
            continue
        # Random days with the same directional assignment.
        draws = rng.integers(0, len(idx), size=n_draws)
        sample = f[idx[draws]]
        if direction == "bearish":
            sample = -sample
        out[h] = {
            "mean": float(sample.mean()),
            "tstat": float(hac_tstat(sample)),
            "n": int(len(idx)),
            "samples": sample,
        }
    return out


def pool_baselines(baselines: list[dict],
                   horizons: list[int] | None = None) -> dict[int, dict]:
    """Pool random-day baseline samples across symbols.

    Returns {horizon: {mean, tstat, n}} (samples dropped).
    """
    horizons = horizons or HORIZONS
    pooled = {}
    for h in horizons:
        samples = []
        for b in baselines:
            s = b.get(h, {}).get("samples", np.array([]))
            if len(s):
                samples.append(s)
        if not samples:
            pooled[h] = {"mean": 0.0, "tstat": 0.0, "n": 0}
            continue
        all_s = np.concatenate(samples)
        pooled[h] = {
            "mean": float(all_s.mean()),
            "tstat": float(hac_tstat(all_s)),
            "n": int(len(all_s)),
        }
    return pooled


# ---------------------------------------------------------------------------
# Pattern event study
# ---------------------------------------------------------------------------
def _empty_horizon() -> dict:
    return {
        "n": 0, "mean": 0.0, "tstat": 0.0,
        "conf0": 0.0, "conf1": 0.0, "conf2": 0.0, "conf3": 0.0, "conf4": 0.0,
        "n_conf1plus": 0, "n_conf2plus": 0,
    }


def _bucket_stats(rets_net: np.ndarray, conf: np.ndarray) -> dict:
    """Compute per-confluence-bucket mean returns + n_conf1plus/n_conf2plus."""
    return {
        "n": int(len(rets_net)),
        "mean": float(rets_net.mean()),
        "tstat": float(hac_tstat(rets_net)),
        "conf0": float(rets_net[conf < 1].mean()) if (conf < 1).any() else 0.0,
        "conf1": float(rets_net[(conf >= 1) & (conf < 2)].mean())
        if ((conf >= 1) & (conf < 2)).any() else 0.0,
        "conf2": float(rets_net[(conf >= 2) & (conf < 3)].mean())
        if ((conf >= 2) & (conf < 3)).any() else 0.0,
        "conf3": float(rets_net[(conf >= 3) & (conf < 4)].mean())
        if ((conf >= 3) & (conf < 4)).any() else 0.0,
        "conf4": float(rets_net[conf >= 4].mean()) if (conf >= 4).any() else 0.0,
        "n_conf1plus": int((conf >= 1).sum()),
        "n_conf2plus": int((conf >= 2).sum()),
    }


def pattern_event_study(df: pd.DataFrame, signals: dict[str, pd.Series],
                        horizons: list[int], cost: float = COST_BPS,
                        symbol: str = "", session_aware: bool = False) -> dict:
    """Event study: for each pattern, compute forward returns (net of costs)
    in the pattern's claimed direction, with weighted-confluence breakdown.

    Returns a dict keyed by pattern name with per-horizon stats AND a flat
    ``events`` list of (symbol, date, horizon, ret_net, conf) tuples used by
    the cross-sectional pooling layer.

    ``session_aware=True`` masks forward windows that cross a session boundary
    (overnight hold) — see ``forward_returns``.
    """
    fwd = forward_returns(df, horizons, session_aware=session_aware)
    results = {}

    for name, sig in signals.items():
        direction = PATTERN_DIRECTIONS.get(name, "neutral")
        if direction == "neutral":
            continue
        sig_arr = sig.to_numpy(dtype=bool)
        idx = np.where(sig_arr)[0]
        # Skip warm-up.
        idx = idx[idx >= MIN_BARS]
        if len(idx) == 0:
            results[name] = {"direction": direction, "n": 0,
                             "horizons": {}, "events": []}
            continue

        # Weighted confluence scores for each signal bar.
        conf_scores = [confluence_score(df, i, direction) for i in idx]

        events: list[tuple] = []
        per_horizon = {}
        for h in horizons:
            f = fwd[h]
            valid = ~np.isnan(f[idx])
            if valid.sum() == 0:
                per_horizon[h] = _empty_horizon()
                continue
            rets = f[idx[valid]]
            if direction == "bearish":
                rets = -rets
            # Net of costs.
            rets_net = rets - cost
            conf = np.array(conf_scores)[valid]
            dates = [df.index[i] for i in idx[valid]]
            for j in range(len(rets_net)):
                events.append((symbol, dates[j], h,
                               float(rets_net[j]), float(conf[j])))
            per_horizon[h] = _bucket_stats(rets_net, conf)

        results[name] = {
            "direction": direction,
            "n": int(len(idx)),
            "horizons": per_horizon,
            "events": events,
        }
    return results


# ---------------------------------------------------------------------------
# Cross-sectional pooling
# ---------------------------------------------------------------------------
def pool_results(results_list: list[dict], horizons: list[int] | None = None) -> dict:
    """Pool pattern events across symbols into a single event study.

    Returns the same shape as ``pattern_event_study`` (with events) so the
    pooled report reuses the same formatter. Combined n and pooled expectancy
    give rare patterns enough samples for meaningful statistics.

    ``horizons`` defaults to the module-level HORIZONS; pass the intraday
    horizons when pooling intraday events.
    """
    horizons = horizons or HORIZONS
    # Collect events per pattern across all symbols.
    per_pattern: dict[str, dict] = {}
    for res in results_list:
        for name, r in res.items():
            if name not in per_pattern:
                per_pattern[name] = {"direction": r["direction"], "events": []}
            per_pattern[name]["events"].extend(r["events"])

    pooled = {}
    for name, p in per_pattern.items():
        events = p["events"]
        if not events:
            pooled[name] = {"direction": p["direction"], "n": 0,
                            "horizons": {}, "events": []}
            continue
        # Unique signal bars = unique (symbol, date) pairs.
        n_signals = len({(e[0], e[1]) for e in events})
        per_horizon = {}
        for h in horizons:
            ev = [e for e in events if e[2] == h]
            if not ev:
                per_horizon[h] = _empty_horizon()
                continue
            rets = np.array([e[3] for e in ev], dtype=float)
            conf = np.array([e[4] for e in ev], dtype=float)
            per_horizon[h] = _bucket_stats(rets, conf)
        pooled[name] = {
            "direction": p["direction"],
            "n": n_signals,
            "horizons": per_horizon,
            "events": events,
        }
    return pooled


def _strip_events(results: dict) -> dict:
    """Drop the per-event lists (and baseline samples) for the JSON dump."""
    out = {}
    for name, r in results.items():
        out[name] = {k: v for k, v in r.items() if k != "events"}
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _baseline_table(baseline: dict, h_suffix: str = "d") -> list[str]:
    lines = ["### Random-day baseline (same directional assignment)",
             "| horizon | mean | tstat | n |",
             "|---|---|---|---|"]
    for h in sorted(baseline.keys()):
        b = baseline[h]
        lines.append(f"| {h}{h_suffix} | {b['mean']*100:+.3f}% | {b['tstat']:+.2f} | {b['n']} |")
    lines.append("")
    return lines


def _pattern_table(results: dict, h_suffix: str = "d") -> list[str]:
    lines = ["### Pattern forward returns (net of costs, claimed direction)",
             "| pattern | dir | n | h | mean | tstat | conf<1 | conf1 | conf2 | conf3 | conf4 | n_conf1+ | n_conf2+ |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, r in results.items():
        if r["n"] == 0:
            lines.append(f"| {name} | {r['direction']} | 0 | - | - | - | - | - | - | - | - | - | - |")
            continue
        for h in sorted(r["horizons"].keys()):
            hr = r["horizons"][h]
            lines.append(
                f"| {name} | {r['direction']} | {hr['n']} | {h}{h_suffix} | "
                f"{hr['mean']*100:+.3f}% | {hr['tstat']:+.2f} | "
                f"{hr['conf0']*100:+.2f}% | {hr['conf1']*100:+.2f}% | "
                f"{hr['conf2']*100:+.2f}% | {hr['conf3']*100:+.2f}% | "
                f"{hr['conf4']*100:+.2f}% | {hr['n_conf1plus']} | {hr['n_conf2plus']} |"
            )
    lines.append("")
    lines.append("_conf buckets = mean net return by weighted confluence score "
                 "(trend=2, volume=1, RSI=1; max 4). conf<1 = no confluence._")
    lines.append("")
    return lines


def _oos_table(results_train: dict, results_test: dict,
               h_suffix: str = "d", train_label: str = "2010-2020",
               test_label: str = "2021-2026") -> list[str]:
    lines = [f"### Out-of-sample split (train < {train_label.split('-')[0]}, "
             f"test >= {test_label.split('-')[0]})",
             "",
             f"#### Train ({train_label})",
             "| pattern | dir | n | h | mean | tstat |",
             "|---|---|---|---|---|---|"]
    for name, r in results_train.items():
        if r["n"] == 0:
            lines.append(f"| {name} | {r['direction']} | 0 | - | - | - |")
            continue
        for h in sorted(r["horizons"].keys()):
            hr = r["horizons"][h]
            lines.append(f"| {name} | {r['direction']} | {hr['n']} | {h}{h_suffix} | "
                         f"{hr['mean']*100:+.3f}% | {hr['tstat']:+.2f} |")
    lines.append("")
    lines.append(f"#### Test ({test_label}, out-of-sample)")
    lines.append("| pattern | dir | n | h | mean | tstat |")
    lines.append("|---|---|---|---|---|---|")
    for name, r in results_test.items():
        if r["n"] == 0:
            lines.append(f"| {name} | {r['direction']} | 0 | - | - | - |")
            continue
        for h in sorted(r["horizons"].keys()):
            hr = r["horizons"][h]
            lines.append(f"| {name} | {r['direction']} | {hr['n']} | {h}{h_suffix} | "
                         f"{hr['mean']*100:+.3f}% | {hr['tstat']:+.2f} |")
    lines.append("")
    lines.append("_A pattern is OOS-validated only if the TEST mean beats the "
                 "test-period baseline and |t| >= Bonferroni threshold "
                 f"({BONFERRONI_T:.2f})._")
    return lines


def format_results(results: dict, baseline: dict, symbol: str,
                   results_train: dict | None = None,
                   results_test: dict | None = None,
                   pooled: bool = False,
                   h_suffix: str = "d",
                   train_label: str = "2010-2020",
                   test_label: str = "2021-2026") -> str:
    """Format results as a readable markdown table, including the OOS split."""
    label = f"POOLED UNIVERSE ({symbol})" if pooled else symbol
    lines = [f"## {label} — Candlestick Pattern Event Study",
             f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · net of "
             f"{COST_BPS*10000:.0f} bps round trip_",
             ""]
    lines += _baseline_table(baseline, h_suffix)
    lines += _pattern_table(results, h_suffix)
    if results_train is not None and results_test is not None:
        lines += _oos_table(results_train, results_test, h_suffix,
                            train_label, test_label)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_symbol(symbol: str, start: str, end: str) -> dict:
    """Run the full event study for one symbol. Returns results dict."""
    logger.info(f"Loading {symbol} daily bars ({start} to {end})...")
    df = load_daily_bars(symbol, start=start, end=end)
    if df.empty or len(df) < 100:
        logger.warning(f"Not enough data for {symbol}: {len(df)} bars")
        return {"symbol": symbol, "error": "insufficient data"}

    df = add_confluence_indicators(df)
    logger.info(f"  {len(df)} bars, {df.index[0].date()} to {df.index[-1].date()}")

    # Detect patterns.
    signals = detect_all_patterns(df, names=CORE_PATTERNS)
    n_signals = {name: int(s.sum()) for name, s in signals.items()}
    logger.info(f"  Signals: {n_signals}")

    # Random-day baseline (bullish direction; bearish mirrors it).
    baseline = random_day_baseline(df, HORIZONS, "bullish")

    # Event study (full sample).
    results = pattern_event_study(df, signals, HORIZONS, symbol=symbol)

    # OOS split: train < 2021, test >= 2021.
    split_date = pd.Timestamp(OOS_SPLIT_DATE)
    df_train = df[df.index < split_date]
    df_test = df[df.index >= split_date]
    signals_train = {k: v[df.index < split_date] for k, v in signals.items()}
    signals_test = {k: v[df.index >= split_date] for k, v in signals.items()}
    results_train = pattern_event_study(df_train, signals_train, HORIZONS,
                                        symbol=symbol)
    results_test = pattern_event_study(df_test, signals_test, HORIZONS,
                                       symbol=symbol)

    return {
        "symbol": symbol,
        "bars": len(df),
        "start": str(df.index[0].date()),
        "end": str(df.index[-1].date()),
        "n_signals": n_signals,
        "baseline": baseline,
        "results": results,
        "results_train": results_train,
        "results_test": results_test,
        "markdown": format_results(results, baseline, symbol,
                                   results_train, results_test),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Candlestick pattern confluence backtest")
    parser.add_argument("--symbol", action="append", default=None,
                        help="Symbol(s) to test (repeatable); default = broad universe")
    parser.add_argument("--start", default="2010-01-01", help="Start date")
    parser.add_argument("--end", default="2026-10-01", help="End date")
    parser.add_argument("--quick", action="store_true",
                        help="Quick mode: fewer bars (2020+)")
    parser.add_argument("--out", default="sideload/backtest_patterns.json",
                        help="Output JSON path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-patterns")

    universe = args.symbol or DEFAULT_UNIVERSE
    if args.quick:
        args.start = "2020-01-01"

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
            md_path = f"sideload/backtest_patterns_{symbol}.md"
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(res["markdown"])
            logger.info(f"  Wrote {md_path}")

        # Pooled universe report.
        if per_symbol_results:
            pooled_results = pool_results(per_symbol_results)
            pooled_train = pool_results(per_symbol_train)
            pooled_test = pool_results(per_symbol_test)
            pooled_baseline = pool_baselines(per_symbol_baselines)
            universe_label = "+".join(all_results.keys())
            pooled_md = format_results(pooled_results, pooled_baseline,
                                       universe_label,
                                       pooled_train, pooled_test,
                                       pooled=True)
            print("\n" + pooled_md + "\n")
            with open("sideload/backtest_patterns_POOLED.md", "w",
                      encoding="utf-8") as f:
                f.write(pooled_md)
            logger.info("  Wrote sideload/backtest_patterns_POOLED.md")

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
        logger.critical(f"Backtest failed: {e}")
        log_exception_to_jira(e, "Pattern Backtest Failure")
        raise


if __name__ == "__main__":
    main()
