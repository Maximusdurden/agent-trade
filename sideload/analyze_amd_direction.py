#!/usr/bin/env python3
"""AMD directional analysis — what drives higher vs lower days.

Builds a per-trading-day dataset of AMD daily bars with every indicator the core
product computes (RSI, SMA, MACD, Bollinger, ATR, volume, regime), then analyzes
what separates days that finish HIGHER (close > prior close) from days that
finish LOWER. Output is the analysis + supporting data — no live trading changes.

This is the first step of the "scale the sideload method to other tickers" plan:
if the method works for AMD, we parameterize --symbol and run it per ticker.

Higher/lower definition (configurable via --direction):
  - "close" (default): direction = "higher" if close > prior close.
  - "gap":           direction = "higher" if OPEN > prior close (gap-up day).
  Amplitude = the % change of the defining price (close-to-close for "close",
  open-vs-prior-close for "gap").

Analysis (locked):
  - Descriptive stats + conditional probabilities (primary).
  - Walk-forward logistic-regression classifier (cross-check, no lookahead).
  - No ML dependency: the classifier is a pure-numpy logistic regression.

News: DEFERRED (NewsAPI free tier caps ~100 results/request — not enough for
3-4 yrs). Technical + volume analysis first; news can be backfilled later.

Usage:
    python -m sideload.analyze_amd_direction --build-dataset
    python -m sideload.analyze_amd_direction --analyze
    python -m sideload.analyze_amd_direction --analyze --classifier
    python -m sideload.analyze_amd_direction --all
    python -m sideload.analyze_amd_direction --all --direction gap
    python -m sideload.analyze_amd_direction --all --symbol NVDA --days 1000
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient
from core.data_provider import DataProvider, classify_regime
from core.discord_notifier import send_discord_message

logger = logging.getLogger("AnalyzeAMDDirection")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
DATA_DIR = os.path.join(OUT_DIR, "data")

# Rolling VWAP window (days) — daily bars have a degenerate session VWAP (one bar
# per day => vwap == typical_price), so we use a rolling volume-weighted average
# price as the daily analog of the core product's VWAP.
VWAP_ROLL_DAYS = 20

# Benchmark symbol for relative-strength KPI (the lane must outperform indices).
BENCHMARK_SYMBOL = "SPY"
# Volume normalization window (days) for volume-vs-avg KPI.
VOLUME_AVG_DAYS = 20

# Feature columns used by the classifier (must be numeric, no NaN after dropna).
CLASSIFIER_FEATURES = [
    "rsi_14",
    "sma20_slope_pct",
    "macd_hist",
    "bollinger_pos",
    "atr_pct",
    "vwap_dist_sigma",
    "gap_pct",
    "volume_pct_change",
    "volume_vs_avg",
    "intraday_range_pct",
    "overnight_share",
    "streak",
    "rel_strength_spy",
    "day_of_week",
]

# Columns that MUST exist on the dataset before descriptive analysis runs.
# ``analyze_descriptive`` / ``write_report`` index these directly; a stale or
# mismatched dataset (e.g. one built by an older ``build_dataset``) would
# otherwise crash with a cryptic ``KeyError`` (the TMCL-1013..1015 signature).
REQUIRED_ANALYSIS_COLUMNS = [
    "direction",
    "amplitude_pct",
    "abs_move_sigma",
    "day_of_week",
    "regime",
    "atr_pct",
    "rsi_14",
    "macd_hist",
    "vwap_dist_sigma",
    "volume_pct_change",
    "sma20_slope_pct",
    "bollinger_pos",
    "gap_pct",
    "streak",
    "volume_vs_avg",
    "overnight_share",
    "rel_strength_spy",
]


def _validate_analysis_columns(df: pd.DataFrame, symbol: str, direction: str) -> None:
    """Raise a clear error if the dataset is missing columns the analysis needs.

    Guards the ``--analyze`` path against a stale/older dataset that lacks the
    columns ``analyze_descriptive`` / ``write_report`` index directly. Without
    this, a missing column surfaces as a bare ``KeyError`` (TMCL-1013..1015).
    """
    missing = [c for c in REQUIRED_ANALYSIS_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Dataset for {symbol} ({direction}) is missing required columns: "
            f"{missing}. Rebuild it with --build-dataset (the saved CSV is stale "
            f"or was produced by an older build_dataset)."
        )


# ---------------------------------------------------------------------------
# Dataset build
# ---------------------------------------------------------------------------
def load_daily(client: AlpacaClient, symbol: str, limit: int) -> pd.DataFrame:
    """Fetch daily OHLCV bars for a symbol, return a clean frame indexed by date."""
    df = client.get_historical_bars(symbol, limit=limit, timeframe_str="day")
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index(level=0, drop=True)
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    df = df[keep].copy()
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


def load_benchmark_daily(client: AlpacaClient, symbol: str, limit: int) -> pd.DataFrame:
    """Fetch benchmark (SPY) daily closes, return a Series of daily % change."""
    df = load_daily(client, symbol, limit=limit)
    if df.empty:
        return pd.Series(dtype=float)
    return df["close"].pct_change() * 100.0


def _rolling_vwap(df: pd.DataFrame, window: int) -> pd.Series:
    """Rolling volume-weighted average price (daily analog of intraday VWAP)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    tp_vol = tp * df["volume"]
    cum_tp_vol = tp_vol.rolling(window=window, min_periods=window).sum()
    cum_vol = df["volume"].rolling(window=window, min_periods=window).sum()
    return cum_tp_vol / cum_vol.replace(0, np.nan)


def _regime_series(df: pd.DataFrame) -> pd.Series:
    """Per-day regime by applying the core classifier on the window ending each day."""
    regimes = []
    for i in range(len(df)):
        window = df.iloc[: i + 1]
        regimes.append(classify_regime(window))
    return pd.Series(regimes, index=df.index)


def build_dataset(client: AlpacaClient, symbol: str, days: int, direction: str = "close") -> pd.DataFrame:
    """Build the per-day directional dataset for a symbol.

    ``direction`` selects the higher/lower definition:
      - "close": higher if close > prior close (amplitude = close-to-close %).
      - "gap":   higher if OPEN > prior close (amplitude = open-vs-prior-close %).
    """
    daily = load_daily(client, symbol, limit=days)
    if len(daily) < 60:
        logger.warning(f"Not enough daily bars for {symbol} ({len(daily)}).")
        return pd.DataFrame()

    dp = DataProvider(client)
    df = dp._add_technical_indicators(daily, symbol=symbol)

    # --- Derived columns ---------------------------------------------------
    df["prev_close"] = df["close"].shift(1)
    df["daily_pct_change"] = (df["close"] - df["prev_close"]) / df["prev_close"] * 100.0
    # Gap (open vs prior close) — the defining price for the "gap" definition.
    df["gap_pct"] = (df["open"] - df["prev_close"]) / df["prev_close"] * 100.0

    if direction == "gap":
        # Higher = the day OPENED above the prior close (gap-up day).
        df["direction"] = np.where(df["open"] > df["prev_close"], "higher", "lower")
        df["amplitude_pct"] = df["gap_pct"]
    else:
        # Higher = the day CLOSED above the prior close.
        df["direction"] = np.where(df["close"] > df["prev_close"], "higher", "lower")
        df["amplitude_pct"] = df["daily_pct_change"]
    # Amplitude, volatility-normalized.
    df["abs_move_sigma"] = df["amplitude_pct"].abs() / df["atr_14"]

    # Rolling VWAP proxy + normalized distance.
    df["vwap_roll_20"] = _rolling_vwap(df, VWAP_ROLL_DAYS)
    df["vwap_dist_sigma"] = (df["close"] - df["vwap_roll_20"]).abs() / df["atr_14"]

    # SMA-20 slope as % of price per bar (trend strength).
    df["sma20_slope_pct"] = df["sma_20"].diff() / df["close"] * 100.0

    # Bollinger position: where close sits within the band (-1..1).
    band = df["bollinger_upper"] - df["bollinger_lower"]
    df["bollinger_pos"] = (2.0 * df["close"] - df["bollinger_upper"] - df["bollinger_lower"]) / band.replace(0, np.nan)

    # Intraday range.
    df["intraday_range_pct"] = (df["high"] - df["low"]) / df["prev_close"] * 100.0
    df["close_vs_open_pct"] = (df["close"] - df["open"]) / df["open"] * 100.0

    # Volume change vs prior day.
    df["prev_volume"] = df["volume"].shift(1)
    df["volume_pct_change"] = (df["volume"] - df["prev_volume"]) / df["prev_volume"] * 100.0
    # Volume vs 20-day average (cleaner "high-volume day" signal).
    df["volume_avg_20"] = df["volume"].rolling(window=VOLUME_AVG_DAYS, min_periods=VOLUME_AVG_DAYS).mean()
    df["volume_vs_avg"] = df["volume"] / df["volume_avg_20"].replace(0, np.nan)

    # Overnight vs intraday decomposition of the daily move.
    # overnight = open vs prior close (gap_pct); intraday = close vs open.
    # overnight_share = |overnight| / (|overnight| + |intraday|) — fraction of the
    # daily move that happens overnight (0=all intraday, 1=all overnight).
    df["overnight_share"] = df["gap_pct"].abs() / (
        df["gap_pct"].abs() + df["close_vs_open_pct"].abs()
    ).replace(0, np.nan)

    # Consecutive-day streak (momentum vs mean-reversion).
    streak_vals = np.zeros(len(df), dtype=int)
    streak = 0
    prev_dir = None
    for i in range(len(df)):
        d = df["direction"].iloc[i]
        if d == prev_dir:
            streak += 1
        else:
            streak = 1
        streak_vals[i] = streak
        prev_dir = d
    df["streak"] = streak_vals

    # Calendar features.
    df["day_of_week"] = df.index.dayofweek
    df["month"] = df.index.month

    # Per-day regime (core classifier on the window ending each day).
    df["regime"] = _regime_series(df)

    # Relative strength vs benchmark (SPY): AMD daily move minus SPY daily move.
    df["rel_strength_spy"] = np.nan
    try:
        bench = load_benchmark_daily(client, BENCHMARK_SYMBOL, limit=days)
        if not bench.empty:
            # Align on date (both are daily closes).
            df["rel_strength_spy"] = df["daily_pct_change"] - bench.reindex(df.index).values
    except Exception as e:
        logger.warning(f"Benchmark (SPY) relative-strength unavailable: {e}")

    # Drop the warm-up rows where indicators aren't defined yet.
    df = df.dropna(subset=["rsi_14", "sma_20", "atr_14", "daily_pct_change"])
    df = df[df["prev_close"] > 0]

    # Keep a clean, ordered column set.
    cols = [
        "open", "high", "low", "close", "volume",
        "prev_close", "daily_pct_change", "amplitude_pct", "direction", "abs_move_sigma",
        "sma_20", "sma_50", "sma20_slope_pct",
        "rsi_14", "macd_line", "macd_signal", "macd_hist",
        "bollinger_upper", "bollinger_lower", "bollinger_pos",
        "atr_14", "atr_pct",
        "vwap_roll_20", "vwap_dist_sigma",
        "gap_pct", "intraday_range_pct", "close_vs_open_pct",
        "volume_pct_change", "volume_vs_avg",
        "overnight_share", "streak", "rel_strength_spy",
        "day_of_week", "month", "regime",
    ]
    df = df[[c for c in cols if c in df.columns]]
    df.index.name = "date"
    return df


def save_dataset(df: pd.DataFrame, symbol: str) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    csv_path = os.path.join(DATA_DIR, f"{symbol.lower()}_directional_dataset.csv")
    json_path = os.path.join(DATA_DIR, f"{symbol.lower()}_directional_dataset.json")
    df.to_csv(csv_path)
    df.reset_index().to_json(json_path, orient="records", date_format="iso")
    logger.info(f"Wrote {csv_path} ({len(df)} rows) and {json_path}")
    return csv_path


# ---------------------------------------------------------------------------
# Descriptive analysis
# ---------------------------------------------------------------------------
def _pct(series: pd.Series) -> float:
    return float(series.mean()) if len(series) else float("nan")


def _bucket_stats(df: pd.DataFrame, feature: str) -> dict:
    """Mean/median of a feature in higher vs lower buckets."""
    out = {}
    for direction in ("higher", "lower"):
        sub = df[df["direction"] == direction][feature].dropna()
        out[direction] = {
            "n": int(len(sub)),
            "mean": float(sub.mean()) if len(sub) else None,
            "median": float(sub.median()) if len(sub) else None,
        }
    return out


def analyze_descriptive(df: pd.DataFrame, direction: str = "close") -> dict:
    """Descriptive stats + conditional probabilities. Returns a JSON-serializable dict."""
    n = len(df)
    higher = df[df["direction"] == "higher"]
    lower = df[df["direction"] == "lower"]
    base_rate = len(higher) / n if n else float("nan")

    # The defining variable IS the label — never treat it as a predictor.
    defining_var = "gap_pct" if direction == "gap" else "daily_pct_change"

    result = {
        "symbol": str(df.attrs.get("symbol", "?")),
        "direction": direction,
        "n_days": n,
        "n_higher": int(len(higher)),
        "n_lower": int(len(lower)),
        "higher_rate": base_rate,
        "amplitude": {
            "amplitude_pct": {
                "higher": {"mean": _pct(higher["amplitude_pct"]), "median": float(higher["amplitude_pct"].median()) if len(higher) else None},
                "lower": {"mean": _pct(lower["amplitude_pct"]), "median": float(lower["amplitude_pct"].median()) if len(lower) else None},
                "all": {"mean": _pct(df["amplitude_pct"]), "std": float(df["amplitude_pct"].std()) if n else None},
            },
            "abs_move_sigma": {
                "higher": {"mean": _pct(higher["abs_move_sigma"])},
                "lower": {"mean": _pct(lower["abs_move_sigma"])},
            },
        },
        "by_day_of_week": {},
        "by_regime": {},
        "feature_buckets": {},
        "conditionals": [],
    }

    # Base rate by day of week.
    for dow in sorted(df["day_of_week"].dropna().unique()):
        sub = df[df["day_of_week"] == dow]
        h = sub[sub["direction"] == "higher"]
        result["by_day_of_week"][str(int(dow))] = {
            "n": int(len(sub)), "higher_rate": len(h) / len(sub) if len(sub) else None,
        }

    # Base rate by regime.
    for regime in sorted(df["regime"].dropna().unique()):
        sub = df[df["regime"] == regime]
        h = sub[sub["direction"] == "higher"]
        result["by_regime"][str(regime)] = {
            "n": int(len(sub)), "higher_rate": len(h) / len(sub) if len(sub) else None,
        }

    # Per-feature bucket comparison.
    numeric_features = [
        "rsi_14", "sma20_slope_pct", "macd_hist", "bollinger_pos", "atr_pct",
        "vwap_dist_sigma", "gap_pct", "intraday_range_pct", "close_vs_open_pct",
        "volume_pct_change", "volume_vs_avg", "overnight_share", "streak",
        "rel_strength_spy",
    ]
    for f in numeric_features:
        if f in df.columns:
            result["feature_buckets"][f] = _bucket_stats(df, f)

    # Conditional probabilities: P(higher | condition) vs base rate.
    conds = [
        ("rsi_14 < 40", df["rsi_14"] < 40),
        ("rsi_14 < 45", df["rsi_14"] < 45),
        ("rsi_14 > 60", df["rsi_14"] > 60),
        ("macd_hist > 0", df["macd_hist"] > 0),
        ("macd_hist < 0", df["macd_hist"] < 0),
        ("vwap_dist_sigma > 1.0", df["vwap_dist_sigma"] > 1.0),
        ("vwap_dist_sigma < 0.5", df["vwap_dist_sigma"] < 0.5),
        ("volume_pct_change > 0", df["volume_pct_change"] > 0),
        ("volume_pct_change < 0", df["volume_pct_change"] < 0),
        ("sma20_slope_pct > 0", df["sma20_slope_pct"] > 0),
        ("sma20_slope_pct < 0", df["sma20_slope_pct"] < 0),
        ("bollinger_pos > 0.5", df["bollinger_pos"] > 0.5),
        ("bollinger_pos < -0.5", df["bollinger_pos"] < -0.5),
        ("atr_pct > median", df["atr_pct"] > df["atr_pct"].median()),
        ("regime == TRENDING_UP", df["regime"] == "TRENDING_UP"),
        ("regime == TRENDING_DOWN", df["regime"] == "TRENDING_DOWN"),
        ("regime == RANGING", df["regime"] == "RANGING"),
        # New KPIs.
        ("streak >= 2", df["streak"] >= 2),
        ("streak >= 3", df["streak"] >= 3),
        ("volume_vs_avg > 1.2", df["volume_vs_avg"] > 1.2),
        ("volume_vs_avg < 0.8", df["volume_vs_avg"] < 0.8),
        ("overnight_share > 0.5", df["overnight_share"] > 0.5),
        ("overnight_share < 0.5", df["overnight_share"] < 0.5),
        ("rel_strength_spy > 0", df["rel_strength_spy"] > 0),
        ("rel_strength_spy < 0", df["rel_strength_spy"] < 0),
    ]
    # For the "close" definition, gap_pct is a legitimate predictor (open vs
    # prior close is NOT the label). For "gap" it IS the label — exclude it.
    if direction == "close":
        conds += [
            ("gap_pct > 0", df["gap_pct"] > 0),
            ("gap_pct < 0", df["gap_pct"] < 0),
        ]
    for label, mask in conds:
        sub = df[mask]
        if len(sub) < 20:
            continue  # flag low-n by omitting; report notes this.
        h = sub[sub["direction"] == "higher"]
        result["conditionals"].append({
            "condition": label,
            "n": int(len(sub)),
            "higher_rate": len(h) / len(sub),
            "lift_vs_base": (len(h) / len(sub)) - base_rate,
            # Expectancy: mean defining-price move for the condition (the KPI
            # that actually drives PnL, not just win rate).
            "mean_move_pct": float(sub["amplitude_pct"].mean()) if len(sub) else None,
        })

    return result


# ---------------------------------------------------------------------------
# Classifier cross-check (pure numpy logistic regression, walk-forward)
# ---------------------------------------------------------------------------
def _standardize(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd == 0] = 1.0
    return (X - mu) / sd, mu, sd


def _logistic_fit(X: np.ndarray, y: np.ndarray, lr: float = 0.1, iters: int = 2000) -> np.ndarray:
    """Batch gradient-descent logistic regression. Returns weights [w0, w1..wk]."""
    Xb = np.column_stack([np.ones(len(X)), X])
    w = np.zeros(Xb.shape[1])
    for _ in range(iters):
        z = Xb @ w
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        grad = Xb.T @ (p - y) / len(y)
        w -= lr * grad
    return w


def _logistic_predict(X: np.ndarray, w: np.ndarray) -> np.ndarray:
    Xb = np.column_stack([np.ones(len(X)), X])
    z = Xb @ w
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def run_classifier(df: pd.DataFrame, folds: int = 5, direction: str = "close") -> dict:
    """Walk-forward logistic regression. Trains on past, tests on future folds."""
    feat = [c for c in CLASSIFIER_FEATURES if c in df.columns]
    # The defining variable IS the label — never feed it to the classifier.
    defining_var = "gap_pct" if direction == "gap" else "daily_pct_change"
    feat = [c for c in feat if c != defining_var]
    data = df.dropna(subset=feat + ["direction"]).copy()
    if len(data) < 100:
        return {"error": "not enough rows after dropna", "n": int(len(data))}

    X = data[feat].to_numpy(dtype=float)
    y = (data["direction"] == "higher").astype(int).to_numpy()
    n = len(data)
    fold_size = n // folds
    if fold_size < 10:
        folds = max(1, n // 10)
        fold_size = n // folds

    oos_preds = []
    oos_true = []
    fold_results = []
    for i in range(folds):
        test_start = i * fold_size
        test_end = n if i == folds - 1 else (i + 1) * fold_size
        if test_start == 0:
            continue  # need at least one training fold before the first test fold
        X_tr, y_tr = X[:test_start], y[:test_start]
        X_te, y_te = X[test_start:test_end], y[test_start:test_end]
        if len(np.unique(y_tr)) < 2 or len(X_tr) < 30:
            continue
        X_tr_s, mu, sd = _standardize(X_tr)
        X_te_s = (X_te - mu) / sd
        w = _logistic_fit(X_tr_s, y_tr)
        pred = (_logistic_predict(X_te_s, w) >= 0.5).astype(int)
        acc = float((pred == y_te).mean()) if len(y_te) else None
        fold_results.append({"fold": i, "train_n": int(len(X_tr)), "test_n": int(len(X_te)), "acc": acc})
        oos_preds.extend(pred.tolist())
        oos_true.extend(y_te.tolist())

    if not oos_true:
        return {"error": "no out-of-sample folds produced", "n": int(n)}

    oos_preds = np.array(oos_preds)
    oos_true = np.array(oos_true)
    base_rate = float(oos_true.mean())
    acc = float((oos_preds == oos_true).mean())

    # Feature weights from a final fit on all standardized data (for direction).
    X_s, _, _ = _standardize(X)
    w_all = _logistic_fit(X_s, y)
    feature_weights = {f: float(w) for f, w in zip(feat, w_all[1:])}

    return {
        "n": int(n),
        "folds": fold_results,
        "oos_n": int(len(oos_true)),
        "oos_accuracy": acc,
        "oos_base_rate": base_rate,
        "accuracy_minus_base": acc - base_rate,
        "feature_weights": feature_weights,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _fmt_pct(x) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def write_report(symbol: str, desc: dict, clf: dict | None, direction: str = "close") -> str:
    lines = []
    lines.append(f"# AMD Directional Analysis — {symbol}")
    lines.append("")
    lines.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · analysis only, no live changes_")
    lines.append("")
    lines.append(f"**Direction definition:** `{direction}` — "
                 + ("higher = close > prior close" if direction == "close"
                    else "higher = OPEN > prior close (gap-up day)"))
    lines.append("")
    lines.append("## Base rates")
    lines.append("")
    lines.append(f"- Days: **{desc['n_days']}** (higher {desc['n_higher']} / lower {desc['n_lower']})")
    lines.append(f"- **Higher rate: {_fmt_pct(desc['higher_rate'])}**")
    lines.append("")
    lines.append("## Amplitude (defining-price % change)")
    lines.append("")
    lines.append("| bucket | mean % | median % |")
    lines.append("|---|---|---|")
    amp = desc["amplitude"]["amplitude_pct"]
    lines.append(f"| higher | {amp['higher']['mean']:.2f} | {amp['higher']['median']:.2f} |")
    lines.append(f"| lower | {amp['lower']['mean']:.2f} | {amp['lower']['median']:.2f} |")
    lines.append(f"| all | {amp['all']['mean']:.2f} (std {amp['all']['std']:.2f}) | — |")
    lines.append("")
    lines.append("## Overnight vs intraday decomposition")
    lines.append("")
    lines.append("_What fraction of the daily move happens overnight (gap) vs intraday._")
    lines.append("")
    lines.append("| bucket | mean overnight_share |")
    lines.append("|---|---|")
    fb = desc["feature_buckets"].get("overnight_share", {})
    if fb:
        lines.append(f"| higher | {fb['higher']['mean']:.2f} |")
        lines.append(f"| lower | {fb['lower']['mean']:.2f} |")
    lines.append("")
    lines.append("_overnight_share = |gap| / (|gap| + |intraday|). 1.0 = move is all overnight; "
                 "0.0 = all intraday. A high share means the daily direction is set by the gap "
                 "(hard to predict from prior-day indicators)._")
    lines.append("")
    lines.append("## Higher rate by day of week")
    lines.append("")
    lines.append("| day | n | higher rate |")
    lines.append("|---|---|---|")
    for dow, v in sorted(desc["by_day_of_week"].items(), key=lambda kv: int(kv[0])):
        lines.append(f"| {dow} | {v['n']} | {_fmt_pct(v['higher_rate'])} |")
    lines.append("")
    lines.append("## Higher rate by regime")
    lines.append("")
    lines.append("| regime | n | higher rate |")
    lines.append("|---|---|---|")
    for regime, v in sorted(desc["by_regime"].items()):
        lines.append(f"| {regime} | {v['n']} | {_fmt_pct(v['higher_rate'])} |")
    lines.append("")
    lines.append("## Feature means by bucket (higher vs lower)")
    lines.append("")
    lines.append("| feature | higher mean | lower mean |")
    lines.append("|---|---|---|")
    for f, v in desc["feature_buckets"].items():
        hm = v["higher"]["mean"]
        lm = v["lower"]["mean"]
        lines.append(f"| {f} | {hm:.3f} | {lm:.3f} |")
    lines.append("")
    lines.append("## Conditional probabilities — P(higher | condition)")
    lines.append("")
    lines.append(f"Base rate: **{_fmt_pct(desc['higher_rate'])}**")
    lines.append("")
    lines.append("| condition | n | P(higher) | lift vs base | mean move % |")
    lines.append("|---|---|---|---|---|")
    for c in sorted(desc["conditionals"], key=lambda x: -abs(x["lift_vs_base"])):
        mm = "n/a" if c.get("mean_move_pct") is None else f"{c['mean_move_pct']:+.2f}"
        lines.append(f"| {c['condition']} | {c['n']} | {_fmt_pct(c['higher_rate'])} | {c['lift_vs_base']:+.1%} | {mm} |")
    lines.append("")
    lines.append("_Low-n conditions (<20) are omitted; treat small-n rows with caution. "
                 "`mean move %` is the expectancy (defining-price move) — the KPI that drives PnL._")
    lines.append("")

    if clf and "error" not in clf:
        lines.append("## Classifier cross-check (walk-forward logistic regression)")
        lines.append("")
        lines.append(f"- Out-of-sample n: **{clf['oos_n']}**")
        lines.append(f"- **OOS accuracy: {_fmt_pct(clf['oos_accuracy'])}** vs base rate {_fmt_pct(clf['oos_base_rate'])}")
        lines.append(f"- Accuracy minus base: **{clf['accuracy_minus_base']:+.1%}**")
        lines.append("")
        lines.append("### Feature weights (final fit; sign = direction of influence)")
        lines.append("")
        lines.append("| feature | weight |")
        lines.append("|---|---|")
        for f, w in sorted(clf["feature_weights"].items(), key=lambda kv: -abs(kv[1])):
            lines.append(f"| {f} | {w:+.4f} |")
        lines.append("")
        lines.append("_Positive weight => pushes toward a HIGHER close; negative => LOWER._")
        lines.append("")

    lines.append("## Candidate rule hypotheses for backtest_amd.py")
    lines.append("")
    lines.append("_These are hypotheses to TEST in the grid backtest — not live changes._")
    lines.append("")
    # Auto-generate hypotheses from the strongest conditional lifts.
    strong = [c for c in desc["conditionals"] if c["n"] >= 50]
    strong.sort(key=lambda x: -abs(x["lift_vs_base"]))
    if strong:
        lines.append("Top single-condition signals (|lift| vs base):")
        lines.append("")
        for c in strong[:6]:
            fav = "HIGHER" if c["lift_vs_base"] > 0 else "LOWER"
            lines.append(f"- `{c['condition']}` → {_fmt_pct(c['higher_rate'])} P(higher) "
                         f"({c['lift_vs_base']:+.1%} lift, n={c['n']}) → favors a **{fav}** close.")
        lines.append("")
        lines.append("Suggested grid additions to `backtest_amd.py` (test, don't ship):")
        lines.append("")
        lines.append("- Add a `gap_pct` gate (e.g. only enter when `gap_pct > 0`).")
        lines.append("- Add a `bollinger_pos` gate (e.g. only enter when `bollinger_pos > 0`).")
        lines.append("- Add an `rsi_14` floor (e.g. skip when `rsi_14 < 40`).")
        lines.append("- Add a `macd_hist` sign filter (e.g. only when `macd_hist > 0`).")
    else:
        lines.append("- No strong single-condition signals found (all |lift| small or low-n).")
    lines.append("")

    suffix = f"_{direction}" if direction != "close" else ""
    path = os.path.join(OUT_DIR, f"{symbol.lower()}{suffix}_directional_analysis.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    logger.info(f"Wrote {path}")
    return path


def _save_json(obj: dict, name: str) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, default=str)
    logger.info(f"Wrote {path}")
    return path


def _notify_discord(symbol: str, msg: str) -> None:
    try:
        send_discord_message(msg)
    except Exception as e:
        logger.warning(f"Discord notify failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="AMD directional analysis")
    parser.add_argument("--symbol", default="AMD", help="Ticker (default AMD)")
    parser.add_argument("--days", type=int, default=1000, help="Calendar days of history (~4yrs)")
    parser.add_argument("--direction", choices=["close", "gap"], default="close",
                        help="Higher/lower definition: 'close' (close>prev close) or "
                             "'gap' (open>prev close). Default 'close'.")
    parser.add_argument("--build-dataset", action="store_true", help="Build the per-day dataset")
    parser.add_argument("--analyze", action="store_true", help="Run descriptive analysis")
    parser.add_argument("--classifier", action="store_true", help="Run classifier cross-check")
    parser.add_argument("--all", action="store_true", help="Build + analyze + classifier")
    parser.add_argument("--no-discord", action="store_true", help="Skip Discord notification")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-analyze-amd-direction")

    do_build = args.build_dataset or args.all
    do_analyze = args.analyze or args.all
    do_clf = args.classifier or args.all

    client = AlpacaClient()
    symbol = args.symbol.upper()
    direction = args.direction
    # Direction-specific file suffix so both definitions coexist.
    suffix = f"_{direction}" if direction != "close" else ""

    try:
        df = pd.DataFrame()
        if do_build:
            df = build_dataset(client, symbol, args.days, direction=direction)
            if df.empty:
                logger.error("No rows produced.")
                return
            df.attrs["symbol"] = symbol
            csv_path = save_dataset(df, f"{symbol}{suffix}")
            if not args.no_discord:
                _notify_discord(symbol, f"AMD directional dataset built ({direction}): {len(df)} rows -> {csv_path}")

        if do_analyze:
            if df.empty:
                ds_path = os.path.join(DATA_DIR, f"{symbol.lower()}{suffix}_directional_dataset.csv")
                if os.path.exists(ds_path):
                    df = pd.read_csv(ds_path, index_col=0, parse_dates=True)
                    df.attrs["symbol"] = symbol
                else:
                    logger.error("No dataset found; run --build-dataset first.")
                    return
            # Guard against a stale/mismatched dataset (TMCL-1013..1015): the
            # analysis indexes required columns directly, so validate them up
            # front and fail with a clear message instead of a bare KeyError.
            _validate_analysis_columns(df, symbol, direction)
            desc = analyze_descriptive(df, direction=direction)
            _save_json(desc, f"{symbol.lower()}{suffix}_directional_buckets.json")

            clf = None
            if do_clf:
                clf = run_classifier(df, direction=direction)
                _save_json(clf, f"{symbol.lower()}{suffix}_directional_classifier.json")

            report_path = write_report(symbol, desc, clf, direction=direction)
            print(f"\nReport: {report_path}\n")
            if not args.no_discord:
                _notify_discord(symbol, f"AMD directional analysis complete ({direction}) -> {report_path}")
    except Exception as e:
        logger.critical(f"AMD directional analysis failed: {e}")
        log_exception_to_jira(e, "AMD Directional Analysis Failure")
        raise


if __name__ == "__main__":
    main()