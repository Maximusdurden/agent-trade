#!/usr/bin/env python3
"""AMD intraday directional analysis — open-to-9:45 momentum → open-to-close.

The daily-bar analysis found NO exploitable edge once predictors were lagged to
t-1 (target leakage fix). The promising path is INTRADAY: use the first ~45
minutes of the session (open-to-9:45 ET momentum, AMD vs SPY) — a genuinely
knowable-at-entry signal — to predict the rest of the day (open-to-close return).

This is the leakage-free continuation of the directional strategy. It answers:
  "If AMD is up X% by 9:45 AM, does it tend to close up or fade?"

Design (leakage-safe):
  - Target: intraday return from OPEN to CLOSE on day t (close_t / open_t - 1).
  - Predictors: computed ONLY from bars up to 9:45 ET on day t (open-to-9:45
    momentum, 9:45 RSI, 9:45 vs prior close, AMD-vs-SPY 9:45 relative strength).
    Nothing after 9:45 leaks into the features.
  - Also lagged daily indicators (t-1 close state) as secondary predictors.

Usage:
    python -m sideload.analyze_amd_intraday --build-dataset
    python -m sideload.analyze_amd_intraday --analyze
    python -m sideload.analyze_amd_intraday --analyze --classifier
    python -m sideload.analyze_amd_intraday --all
    python -m sideload.analyze_amd_intraday --all --symbol NVDA
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient
from core.data_provider import DataProvider
from core.discord_notifier import send_discord_message

logger = logging.getLogger("AnalyzeAMDIntraday")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
DATA_DIR = os.path.join(OUT_DIR, "data")
ET = ZoneInfo("America/New_York")

# Intraday bar interval and the "signal cutoff" time (ET).
INTRADAY_INTERVAL = "5min"
# The signal is computed from bars up to this time (inclusive).
SIGNAL_CUTOFF = dtime(9, 45)
# Benchmark for relative-strength-at-9:45.
BENCHMARK_SYMBOL = "SPY"

# Classifier features (all knowable at 9:45 ET or earlier).
CLASSIFIER_FEATURES = [
    "open_to_945_pct",       # AMD open->9:45 momentum
    "rel_strength_945",      # AMD 9:45 momentum - SPY 9:45 momentum
    "rsi_945",               # RSI at 9:45
    "gap_pct",               # open vs prior close (t-1 close known)
    "prev_day_move_pct",     # t-1 close-to-close move
    "prev_rsi",              # t-1 close RSI
    "volume_vs_avg",         # t-1 volume vs 20d avg
    "day_of_week",
]


def _to_et(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(ET)


def load_intraday(client: AlpacaClient, symbol: str, days_back: int) -> pd.DataFrame:
    """Fetch intraday OHLCV bars, return a frame with an ET DatetimeIndex."""
    df = client.get_historical_bars_paginated(
        symbol, timeframe_str=INTRADAY_INTERVAL, days_back=days_back)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index(level=0, drop=True)
    df.index = pd.to_datetime(df.index)
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ET)
    df = df.sort_index()
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    df = df[keep].copy()
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


def load_daily(client: AlpacaClient, symbol: str, limit: int) -> pd.DataFrame:
    """Fetch daily bars, return a clean frame indexed by ET date."""
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


def _day_open_close(intraday: pd.DataFrame) -> pd.DataFrame:
    """Per-day open and close from intraday bars (tz-naive ET date index)."""
    intraday = intraday.copy()
    intraday["et_date"] = intraday.index.date
    g = intraday.groupby("et_date")
    out = pd.DataFrame({
        "open": g["open"].first(),
        "close": g["close"].last(),
    })
    out.index = pd.to_datetime(out.index)
    return out


def _price_at(intraday: pd.DataFrame, day: pd.Timestamp, cutoff: dtime) -> float | None:
    """Return the intraday close nearest to (but not after) ``cutoff`` on ``day``."""
    day_et = day.tz_convert(ET) if day.tzinfo is not None else day.tz_localize(ET)
    start = day_et.replace(hour=9, minute=0, second=0, microsecond=0)
    end = day_et.replace(hour=cutoff.hour, minute=cutoff.minute, second=0, microsecond=0)
    window = intraday[(intraday.index >= start) & (intraday.index <= end)]
    if window.empty:
        return None
    return float(window["close"].iloc[-1])


def _rsi_at(intraday: pd.DataFrame, day: pd.Timestamp, cutoff: dtime) -> float | None:
    """RSI-14 computed on bars up to ``cutoff`` on ``day`` (uses prior days too)."""
    day_et = day.tz_convert(ET) if day.tzinfo is not None else day.tz_localize(ET)
    end = day_et.replace(hour=cutoff.hour, minute=cutoff.minute, second=0, microsecond=0)
    window = intraday[intraday.index <= end]
    if len(window) < 15:
        return None
    close = window["close"]
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0))
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs = avg_gain / np.where(avg_loss == 0, 0.00001, avg_loss)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def build_dataset(client: AlpacaClient, symbol: str, days_back: int = 730) -> pd.DataFrame:
    """Build the per-day intraday directional dataset.

    One row per trading day. Target = open-to-close return. Predictors are
    computed ONLY from bars up to 9:45 ET (open-to-9:45 momentum, 9:45 RSI,
    AMD-vs-SPY 9:45 relative strength) plus lagged daily state (t-1 close).
    """
    intraday = load_intraday(client, symbol, days_back)
    if intraday.empty:
        logger.warning(f"No intraday bars for {symbol}.")
        return pd.DataFrame()

    # Daily open/close per day.
    day_oc = _day_open_close(intraday)
    if len(day_oc) < 60:
        logger.warning(f"Not enough days for {symbol} ({len(day_oc)}).")
        return pd.DataFrame()

    # Daily indicators (for lagged t-1 state).
    daily = load_daily(client, symbol, limit=days_back)
    dp = DataProvider(client)
    daily_ind = dp._add_technical_indicators(daily, symbol=symbol)
    # Normalize the daily index to tz-naive dates so it matches the intraday
    # day index (which is tz-naive dates from _day_open_close).
    if daily_ind.index.tz is not None:
        daily_ind.index = daily_ind.index.tz_convert(ET).tz_localize(None).normalize()
    daily_ind["prev_close"] = daily_ind["close"].shift(1)
    daily_ind["daily_pct_change"] = (daily_ind["close"] - daily_ind["prev_close"]) / daily_ind["prev_close"] * 100.0
    daily_ind["volume_avg_20"] = daily_ind["volume"].rolling(window=20, min_periods=20).mean()
    daily_ind["volume_vs_avg"] = daily_ind["volume"] / daily_ind["volume_avg_20"].replace(0, np.nan)

    # Benchmark (SPY) intraday for 9:45 relative strength.
    bench_945 = None
    try:
        bench_intraday = load_intraday(client, BENCHMARK_SYMBOL, days_back)
        if not bench_intraday.empty:
            bench_oc = _day_open_close(bench_intraday)
            bench_945 = {}
            for d in bench_oc.index:
                p = _price_at(bench_intraday, d, SIGNAL_CUTOFF)
                if p is not None:
                    bench_945[d] = (p / bench_oc.loc[d, "open"] - 1.0) * 100.0
    except Exception as e:
        logger.warning(f"Benchmark (SPY) intraday unavailable: {e}")

    rows = []
    for day in day_oc.index:
        o = float(day_oc.loc[day, "open"])
        c = float(day_oc.loc[day, "close"])
        if o <= 0:
            continue
        # Target: open-to-close return.
        intraday_ret = (c / o - 1.0) * 100.0
        # Signal: open-to-9:45 momentum.
        p945 = _price_at(intraday, day, SIGNAL_CUTOFF)
        if p945 is None:
            continue
        open_to_945 = (p945 / o - 1.0) * 100.0
        rsi_945 = _rsi_at(intraday, day, SIGNAL_CUTOFF)

        # Lagged daily state (t-1 close).
        day_prev = day - pd.Timedelta(days=1)
        prev_row = None
        if day_prev in daily_ind.index:
            prev_row = daily_ind.loc[day_prev]
        prev_day_move = float(prev_row["daily_pct_change"]) if prev_row is not None and not pd.isna(prev_row["daily_pct_change"]) else None
        prev_rsi = float(prev_row["rsi_14"]) if prev_row is not None and not pd.isna(prev_row["rsi_14"]) else None
        prev_vol = float(prev_row["volume_vs_avg"]) if prev_row is not None and not pd.isna(prev_row["volume_vs_avg"]) else None
        gap_pct = (o / float(prev_row["close"]) - 1.0) * 100.0 if prev_row is not None and prev_row["close"] > 0 else None

        # AMD-vs-SPY 9:45 relative strength.
        rel_strength_945 = None
        if bench_945 is not None and day in bench_945:
            rel_strength_945 = open_to_945 - bench_945[day]

        rows.append({
            "date": str(day.date()),
            "open": o,
            "close": c,
            "intraday_ret_pct": intraday_ret,
            "direction": "higher" if intraday_ret > 0 else "lower",
            "open_to_945_pct": open_to_945,
            "rsi_945": rsi_945,
            "rel_strength_945": rel_strength_945,
            "gap_pct": gap_pct,
            "prev_day_move_pct": prev_day_move,
            "prev_rsi": prev_rsi,
            "volume_vs_avg": prev_vol,
            "day_of_week": day.dayofweek,
        })

    out = pd.DataFrame(rows)
    out = out.dropna(subset=["open_to_945_pct", "intraday_ret_pct"])
    out.index = pd.to_datetime(out["date"])
    out.index.name = "date"
    return out


def save_dataset(df: pd.DataFrame, symbol: str) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    csv_path = os.path.join(DATA_DIR, f"{symbol.lower()}_intraday_directional_dataset.csv")
    json_path = os.path.join(DATA_DIR, f"{symbol.lower()}_intraday_directional_dataset.json")
    df.to_csv(csv_path)
    # The index is named 'date' AND there's a 'date' column — drop the column
    # BEFORE reset_index so the JSON doesn't get a duplicate 'date'.
    out = df.drop(columns=["date"], errors="ignore").reset_index()
    out.to_json(json_path, orient="records", date_format="iso")
    logger.info(f"Wrote {csv_path} ({len(df)} rows) and {json_path}")
    return csv_path


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def _pct(series: pd.Series) -> float:
    return float(series.mean()) if len(series) else float("nan")


def analyze_descriptive(df: pd.DataFrame) -> dict:
    n = len(df)
    higher = df[df["direction"] == "higher"]
    lower = df[df["direction"] == "lower"]
    base_rate = len(higher) / n if n else float("nan")

    result = {
        "symbol": str(df.attrs.get("symbol", "?")),
        "n_days": n,
        "n_higher": int(len(higher)),
        "n_lower": int(len(lower)),
        "higher_rate": base_rate,
        "amplitude": {
            "intraday_ret_pct": {
                "higher": {"mean": _pct(higher["intraday_ret_pct"]), "median": float(higher["intraday_ret_pct"].median()) if len(higher) else None},
                "lower": {"mean": _pct(lower["intraday_ret_pct"]), "median": float(lower["intraday_ret_pct"].median()) if len(lower) else None},
                "all": {"mean": _pct(df["intraday_ret_pct"]), "std": float(df["intraday_ret_pct"].std()) if n else None},
            },
        },
        "feature_buckets": {},
        "conditionals": [],
    }

    numeric_features = [
        "open_to_945_pct", "rsi_945", "rel_strength_945", "gap_pct",
        "prev_day_move_pct", "prev_rsi", "volume_vs_avg",
    ]
    for f in numeric_features:
        if f in df.columns:
            result["feature_buckets"][f] = _bucket_stats(df, f)

    conds = [
        ("open_to_945_pct > 0", df["open_to_945_pct"] > 0),
        ("open_to_945_pct < 0", df["open_to_945_pct"] < 0),
        ("open_to_945_pct > 0.5", df["open_to_945_pct"] > 0.5),
        ("open_to_945_pct < -0.5", df["open_to_945_pct"] < -0.5),
        ("rel_strength_945 > 0", df["rel_strength_945"] > 0),
        ("rel_strength_945 < 0", df["rel_strength_945"] < 0),
        ("rsi_945 < 40", df["rsi_945"] < 40),
        ("rsi_945 > 60", df["rsi_945"] > 60),
        ("gap_pct > 0", df["gap_pct"] > 0),
        ("gap_pct < 0", df["gap_pct"] < 0),
        ("prev_day_move_pct > 0", df["prev_day_move_pct"] > 0),
        ("prev_day_move_pct < 0", df["prev_day_move_pct"] < 0),
        ("prev_rsi < 40", df["prev_rsi"] < 40),
        ("volume_vs_avg > 1.2", df["volume_vs_avg"] > 1.2),
    ]
    for label, mask in conds:
        sub = df[mask]
        if len(sub) < 20:
            continue
        h = sub[sub["direction"] == "higher"]
        l = sub[sub["direction"] == "lower"]
        higher_rate = len(h) / len(sub)
        result["conditionals"].append({
            "condition": label,
            "n": int(len(sub)),
            "higher_rate": higher_rate,
            "lower_rate": len(l) / len(sub),
            "lift_vs_base": higher_rate - base_rate,
            "lower_lift_vs_base": (len(l) / len(sub)) - (1.0 - base_rate),
            "mean_move_pct": float(sub["intraday_ret_pct"].mean()) if len(sub) else None,
            "downside_mean_move_pct": float(l["intraday_ret_pct"].mean()) if len(l) else None,
        })
    return result


def _bucket_stats(df: pd.DataFrame, feature: str) -> dict:
    out = {}
    for direction in ("higher", "lower"):
        sub = df[df["direction"] == direction][feature].dropna()
        out[direction] = {
            "n": int(len(sub)),
            "mean": float(sub.mean()) if len(sub) else None,
            "median": float(sub.median()) if len(sub) else None,
        }
    return out


# ---------------------------------------------------------------------------
# Classifier (pure numpy logistic regression, walk-forward)
# ---------------------------------------------------------------------------
def _standardize(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd == 0] = 1.0
    return (X - mu) / sd, mu, sd


def _logistic_fit(X: np.ndarray, y: np.ndarray, lr: float = 0.1, iters: int = 2000) -> np.ndarray:
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


def run_classifier(df: pd.DataFrame, folds: int = 5) -> dict:
    feat = [c for c in CLASSIFIER_FEATURES if c in df.columns]
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
    oos_preds, oos_true, fold_results = [], [], []
    for i in range(folds):
        test_start = i * fold_size
        test_end = n if i == folds - 1 else (i + 1) * fold_size
        if test_start == 0:
            continue
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


def write_report(symbol: str, desc: dict, clf: dict | None) -> str:
    lines = []
    lines.append(f"# AMD Intraday Directional Analysis — {symbol}")
    lines.append("")
    lines.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · analysis only, no live changes_")
    lines.append("")
    lines.append("**Signal:** open-to-9:45 ET momentum (knowable at entry) → **target:** open-to-close return.")
    lines.append("")
    lines.append("## Base rates")
    lines.append("")
    lines.append(f"- Days: **{desc['n_days']}** (higher {desc['n_higher']} / lower {desc['n_lower']})")
    lines.append(f"- **Higher rate: {_fmt_pct(desc['higher_rate'])}**")
    lines.append("")
    lines.append("## Amplitude (open-to-close % change)")
    lines.append("")
    lines.append("| bucket | mean % | median % |")
    lines.append("|---|---|---|")
    amp = desc["amplitude"]["intraday_ret_pct"]
    h_mean = "n/a" if amp["higher"]["mean"] is None else f"{amp['higher']['mean']:.2f}"
    h_med = "n/a" if amp["higher"]["median"] is None else f"{amp['higher']['median']:.2f}"
    l_mean = "n/a" if amp["lower"]["mean"] is None else f"{amp['lower']['mean']:.2f}"
    l_med = "n/a" if amp["lower"]["median"] is None else f"{amp['lower']['median']:.2f}"
    a_mean = "n/a" if amp["all"]["mean"] is None else f"{amp['all']['mean']:.2f}"
    a_std = "n/a" if amp["all"]["std"] is None else f"{amp['all']['std']:.2f}"
    lines.append(f"| higher | {h_mean} | {h_med} |")
    lines.append(f"| lower | {l_mean} | {l_med} |")
    lines.append(f"| all | {a_mean} (std {a_std}) | — |")
    lines.append("")
    lines.append("## Feature means by bucket (higher vs lower)")
    lines.append("")
    lines.append("| feature | higher mean | lower mean |")
    lines.append("|---|---|---|")
    for f, v in desc["feature_buckets"].items():
        hm = v["higher"]["mean"]
        lm = v["lower"]["mean"]
        hm_s = "n/a" if hm is None else f"{hm:.3f}"
        lm_s = "n/a" if lm is None else f"{lm:.3f}"
        lines.append(f"| {f} | {hm_s} | {lm_s} |")
    lines.append("")
    lines.append("## Conditional probabilities — P(higher | condition)")
    lines.append("")
    hr = desc["higher_rate"]
    lower_rate = None if hr is None else 1.0 - hr
    lines.append(f"Base rate: **{_fmt_pct(hr)}** (lower {_fmt_pct(lower_rate)})")
    lines.append("")
    lines.append("| condition | n | P(higher) | P(lower) | mean move % |")
    lines.append("|---|---|---|---|---|")
    for c in sorted(desc["conditionals"], key=lambda x: -abs(x["lift_vs_base"])):
        mm = "n/a" if c.get("mean_move_pct") is None else f"{c['mean_move_pct']:+.2f}"
        lines.append(f"| {c['condition']} | {c['n']} | {_fmt_pct(c['higher_rate'])} | {_fmt_pct(c['lower_rate'])} | {mm} |")
    lines.append("")
    lines.append("_Low-n conditions (<20) are omitted. `mean move %` is the open-to-close expectancy._")
    lines.append("")
    lines.append("## Short-side signals — P(lower | condition) ranked by downside expectancy")
    lines.append("")
    lines.append("| condition | n | P(lower) | lower lift vs base | downside mean move % |")
    lines.append("|---|---|---|---|---|")
    shorts = [c for c in desc["conditionals"] if c.get("lower_lift_vs_base") is not None]
    shorts.sort(key=lambda x: -abs(x["lower_lift_vs_base"]))
    for c in shorts[:12]:
        dm = "n/a" if c.get("downside_mean_move_pct") is None else f"{c['downside_mean_move_pct']:+.2f}"
        lines.append(f"| {c['condition']} | {c['n']} | {_fmt_pct(c['lower_rate'])} | {c['lower_lift_vs_base']:+.1%} | {dm} |")
    lines.append("")
    lines.append("_`downside mean move %` = mean open-to-close move on LOWER days only (expected short gain)._")
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

    path = os.path.join(OUT_DIR, f"{symbol.lower()}_intraday_directional_analysis.md")
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
    parser = argparse.ArgumentParser(description="AMD intraday directional analysis")
    parser.add_argument("--symbol", default="AMD", help="Ticker (default AMD)")
    parser.add_argument("--days", type=int, default=730, help="Calendar days of history (~2yrs)")
    parser.add_argument("--build-dataset", action="store_true", help="Build the per-day dataset")
    parser.add_argument("--analyze", action="store_true", help="Run descriptive analysis")
    parser.add_argument("--classifier", action="store_true", help="Run classifier cross-check")
    parser.add_argument("--all", action="store_true", help="Build + analyze + classifier")
    parser.add_argument("--no-discord", action="store_true", help="Skip Discord notification")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-analyze-amd-intraday")

    do_build = args.build_dataset or args.all
    do_analyze = args.analyze or args.all
    do_clf = args.classifier or args.all

    client = AlpacaClient()
    symbol = args.symbol.upper()

    try:
        df = pd.DataFrame()
        if do_build:
            df = build_dataset(client, symbol, args.days)
            if df.empty:
                logger.error("No rows produced.")
                return
            df.attrs["symbol"] = symbol
            csv_path = save_dataset(df, symbol)
            if not args.no_discord:
                _notify_discord(symbol, f"AMD intraday dataset built: {len(df)} rows -> {csv_path}")

        if do_analyze:
            if df.empty:
                ds_path = os.path.join(DATA_DIR, f"{symbol.lower()}_intraday_directional_dataset.csv")
                if os.path.exists(ds_path):
                    df = pd.read_csv(ds_path, index_col=0, parse_dates=True)
                    df.attrs["symbol"] = symbol
                else:
                    logger.error("No dataset found; run --build-dataset first.")
                    return
            desc = analyze_descriptive(df)
            _save_json(desc, f"{symbol.lower()}_intraday_directional_buckets.json")

            clf = None
            if do_clf:
                clf = run_classifier(df)
                _save_json(clf, f"{symbol.lower()}_intraday_directional_classifier.json")

            report_path = write_report(symbol, desc, clf)
            print(f"\nReport: {report_path}\n")
            if not args.no_discord:
                _notify_discord(symbol, f"AMD intraday analysis complete -> {report_path}")
    except Exception as e:
        logger.critical(f"AMD intraday analysis failed: {e}")
        log_exception_to_jira(e, "AMD Intraday Directional Analysis Failure")
        raise


if __name__ == "__main__":
    main()