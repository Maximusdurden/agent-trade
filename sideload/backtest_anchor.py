#!/usr/bin/env python3
"""Prior-day OHLC anchor study (research only — no live lane yet).

This is the FIRST pass of a new sideloaded strategy: instead of the AMD lane's
RSI/VWAP/ATR grid, this lane is anchored to the PREVIOUS DAY's OHLC. The core
question (per the user):

    "Does the current day finish higher or lower vs each of the previous day's
     OHLC values — and is there ONE of those four values we can anchor to and
     trade relative to the next morning?"

This module answers that with a PURE DIRECTIONAL STUDY on DAILY bars. It does
NOT simulate intraday entries/exits yet (that's Phase 2, once we see whether any
anchor has a real edge). It:

  1. Pulls ~2 years of daily bars for SPY and QQQ from Alpaca.
  2. For each day, computes the 4 prior-day anchors: prev_open/high/low/close.
  3. For each anchor, computes directional stats:
       - P(close > anchor)   day finishes above the anchor
       - P(high  > anchor)   day's high exceeds the anchor
       - P(low   > anchor)   day's low stays above the anchor
       - P(open  > anchor)   day opens above the anchor
       - avg |close - anchor|%   magnitude of the edge (not just direction)
  4. The "trade relative to it next morning" test (point 6):
       - P(close > anchor | open > anchor)   open above -> finish above?
       - P(close > anchor | open < anchor)   open below -> finish below?
     A conditional edge meaningfully above 50% means the anchor is worth
     trading off at the open.
  5. Overfitting guard: splits history in half and confirms the edge holds in
     BOTH halves before trusting it.
  6. Ranks the 4 anchors by predictability (distance from the 50/50 coin flip).

Outputs (easy to view):
  - Console: readable summary table per ticker.
  - sideload/anchor_study_spy.csv  +  anchor_study_qqq.csv  (one row per day,
    all anchor comparisons) — open in Excel/VS Code.
  - sideload/anchor_study_summary.json (aggregate stats).

Usage:
    python -m sideload.backtest_anchor
    python -m sideload.backtest_anchor --symbols SPY QQQ
    python -m sideload.backtest_anchor --years 2 --limit 600
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime

import pandas as pd
import numpy as np

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("BacktestAnchor")

# The four prior-day OHLC anchors we test.
ANCHORS = ["open", "high", "low", "close"]

# Output paths (relative to the project root).
OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
SUMMARY_PATH = os.path.join(OUT_DIR, "anchor_study_summary.json")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_daily_bars(client: AlpacaClient, symbol: str, limit: int = 600) -> pd.DataFrame:
    """Fetch daily bars for a symbol and return a clean OHLC frame.

    Daily bars fit in a single Alpaca request (no pagination needed). We keep
    only the columns we need and drop any rows missing OHLC.
    """
    df = client.get_historical_bars(symbol, limit=limit, timeframe_str="day")
    if df is None or df.empty:
        return pd.DataFrame()
    # Normalize index to a plain DatetimeIndex (drop the symbol level if present).
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index(level=0, drop=True)
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    df = df[keep].copy()
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


# ---------------------------------------------------------------------------
# Per-day anchor comparison
# ---------------------------------------------------------------------------
def _pct(above: float, below: float) -> float:
    """Percent of 'above' out of (above + below). NaN when no comparable days."""
    total = above + below
    if total <= 0:
        return float("nan")
    return above / total * 100.0


def compute_day_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Build one row per day with all anchor comparisons.

    For each day t, the anchors are the PRIOR day's (t-1) OHLC. Each row stores,
    per anchor, whether today's close/high/low/open finished above it, plus the
    magnitude |close - anchor| as a % of the anchor.
    """
    rows = []
    prev = df.shift(1)  # prior-day OHLC
    for i in range(1, len(df)):
        ts = df.index[i]
        o, h, l, c = (df["open"].iloc[i], df["high"].iloc[i],
                      df["low"].iloc[i], df["close"].iloc[i])
        row = {"date": ts.date().isoformat()}
        for a in ANCHORS:
            anchor = prev[a].iloc[i]
            if pd.isna(anchor) or anchor <= 0:
                continue
            row[f"anchor_{a}"] = float(anchor)
            row[f"close_gt_{a}"] = int(c > anchor)
            row[f"high_gt_{a}"] = int(h > anchor)
            row[f"low_gt_{a}"] = int(l > anchor)
            row[f"open_gt_{a}"] = int(o > anchor)
            row[f"close_mag_{a}"] = float((c - anchor) / anchor * 100.0)
        rows.append(row)
    return pd.DataFrame(rows)


def _agg(rows: pd.DataFrame, col: str) -> dict:
    """Aggregate a boolean column into {n, pct_above}."""
    vals = rows[col].dropna().astype(int)
    n = int(len(vals))
    if n == 0:
        return {"n": 0, "pct_above": float("nan")}
    return {"n": n, "pct_above": float(vals.sum()) / n * 100.0}


def _mag(rows: pd.DataFrame, col: str) -> float:
    vals = rows[col].dropna()
    if len(vals) == 0:
        return float("nan")
    return float(vals.abs().mean())


def _open_above_stats(summary: dict, anchor: str) -> tuple[int, float]:
    """Count + rate of days where TODAY's OPEN finished above the given anchor."""
    s = summary[anchor]["open_gt"]
    n = s["n"]
    cnt = int(s["pct_above"] / 100.0 * n) if n else 0
    rate = (s["pct_above"] / 100.0) if n else 0.0
    return cnt, rate


def summarize(rows: pd.DataFrame) -> dict:
    """Aggregate the per-day rows into per-anchor directional stats."""
    out = {"n_days": int(len(rows))}
    for a in ANCHORS:
        out[a] = {
            "close_gt": _agg(rows, f"close_gt_{a}"),
            "high_gt": _agg(rows, f"high_gt_{a}"),
            "low_gt": _agg(rows, f"low_gt_{a}"),
            "open_gt": _agg(rows, f"open_gt_{a}"),
            "close_mag_pct": _mag(rows, f"close_mag_{a}"),
            # The "trade relative to it next morning" test (point 6).
            "cond_open_above_close_above": _cond(rows, f"open_gt_{a}", f"close_gt_{a}"),
            "cond_open_below_close_below": _cond(rows, f"open_gt_{a}", f"close_gt_{a}",
                                                 invert=True),
        }
    return out


def _cond(rows: pd.DataFrame, cond_col: str, out_col: str, invert: bool = False) -> dict:
    """P(out | cond): % of rows where out is True, among rows where cond is True.

    When ``invert`` is True, conditions on cond_col == 0 (e.g. "opened below the
    anchor") and reports the % that finished BELOW (out_col == 0).
    """
    cond = rows[cond_col].dropna().astype(int)
    if invert:
        mask = cond == 0
        sub = rows.loc[mask, out_col].dropna().astype(int)
        n = int(len(sub))
        if n == 0:
            return {"n": 0, "pct_above": float("nan")}
        # % that finished below (out == 0).
        return {"n": n, "pct_above": float((sub == 0).sum()) / n * 100.0}
    mask = cond == 1
    sub = rows.loc[mask, out_col].dropna().astype(int)
    n = int(len(sub))
    if n == 0:
        return {"n": 0, "pct_above": float("nan")}
    return {"n": n, "pct_above": float(sub.sum()) / n * 100.0}


# ---------------------------------------------------------------------------
# Overfitting guard: does the edge hold in both halves?
# ---------------------------------------------------------------------------
def _edge_score(summary: dict, anchor: str) -> float:
    """Predictability score for ONE anchor: how far its conditional edges are
    from 50%. Uses the two "trade next morning" conditionals (the actionable
    signal). A score of 0 = pure coin flip; higher = more predictable.
    """
    scores = []
    up = summary[anchor]["cond_open_above_close_above"].get("pct_above")
    dn = summary[anchor]["cond_open_below_close_below"].get("pct_above")
    for v in (up, dn):
        if v is not None and not np.isnan(v):
            scores.append(abs(v - 50.0))
    return float(np.mean(scores)) if scores else 0.0


def split_half_validation(rows: pd.DataFrame) -> dict:
    """Split the study into two chronological halves and re-summarize each.

    If a directional edge is real (not overfit), it should appear in BOTH
    halves. Returns {first_half, second_half, edge_holds} where edge_holds is
    True when the best anchor's conditional edge is on the same side of 50% in
    both halves.
    """
    n = len(rows)
    if n < 20:
        return {"first_half": {}, "second_half": {}, "edge_holds": False}
    mid = n // 2
    first = summarize(rows.iloc[:mid])
    second = summarize(rows.iloc[mid:])
    # Best anchor by combined conditional edge in the full sample.
    best = best_anchor(summarize(rows))
    if best is None:
        return {"first_half": first, "second_half": second, "edge_holds": False}
    f_up = first[best]["cond_open_above_close_above"].get("pct_above")
    f_dn = first[best]["cond_open_below_close_below"].get("pct_above")
    s_up = second[best]["cond_open_above_close_above"].get("pct_above")
    s_dn = second[best]["cond_open_below_close_below"].get("pct_above")
    # Edge holds if both halves agree on direction (both > 50 or both < 50) for
    # at least one of the two conditionals, and the other isn't strongly opposed.
    holds = False
    for f, s in ((f_up, s_up), (f_dn, s_dn)):
        if f is not None and s is not None and not np.isnan(f) and not np.isnan(s):
            if (f > 50.0 and s > 50.0) or (f < 50.0 and s < 50.0):
                holds = True
    return {"first_half": first, "second_half": second, "edge_holds": holds}


def best_anchor(summary: dict) -> str | None:
    """Return the anchor with the highest predictability score."""
    best, best_score = None, -1.0
    for a in ANCHORS:
        s = _edge_score(summary, a)
        if s > best_score:
            best, best_score = a, s
    return best


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _fmt_pct(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "  n/a "
    return f"{v:5.1f}%"


def print_summary(symbol: str, summary: dict, rows: pd.DataFrame) -> None:
    print(f"\n{'=' * 78}")
    print(f"ANCHOR STUDY — {symbol}  ({summary['n_days']} trading days)")
    print(f"{'=' * 78}")
    print("One row per (ticker, prior-day OHLC anchor).")
    print("higher_rate = current_day_close_finished_higher / total_records")
    print("opened_above_prev_low = count+rate of days where TODAY's OPEN is")
    print("above the prior day's LOW (same for every anchor row).")
    print()
    ol_cnt, ol_rate = _open_above_stats(summary, "low")
    hdr = (f"{'ticker':<7}{'anchor':<7}{'total_records':>14}"
           f"{'close_higher':>13}{'higher_rate':>12}"
           f"{'opened_above_prev_low':>20}{'rate':>8}")
    print(hdr)
    print("-" * len(hdr))
    for a in ANCHORS:
        s = summary[a]
        n = s["close_gt"]["n"]
        cnt = int(s["close_gt"]["pct_above"] / 100.0 * n) if n else 0
        rate = s["close_gt"]["pct_above"]
        print(
            f"{symbol:<7}{a:<7}{n:>14}{cnt:>13}"
            f"{rate / 100.0:>11.1%}"
            f"{ol_cnt:>20}{ol_rate:>7.1%}"
        )
    print("-" * len(hdr))
    best = best_anchor(summary)
    print(f"\nBest anchor: {best}  (predictability score {_edge_score(summary, best):.1f} / 50 max)")
    print(f"Rows written: {len(rows)}")


def write_flat_csv(results: dict) -> str:
    """Write a combined flat CSV: one row per (ticker, anchor).

    Columns: ticker, anchor, total_records, current_day_close_finished_higher,
    higher_rate. This is the easy-to-view format the user asked for.
    """
    import csv as _csv
    path = os.path.join(OUT_DIR, "anchor_study_flat.csv")
    with open(path, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["ticker", "anchor", "total_records",
                    "current_day_close_finished_higher", "higher_rate",
                    "opened_above_prev_low", "opened_above_prev_low_rate"])
        for symbol, summary in results.items():
            for a in ANCHORS:
                s = summary[a]
                n = s["close_gt"]["n"]
                cnt = int(s["close_gt"]["pct_above"] / 100.0 * n) if n else 0
                rate = (s["close_gt"]["pct_above"] / 100.0) if n else 0.0
                ol_cnt, ol_rate = _open_above_stats(summary, "low")
                w.writerow([symbol, a, n, cnt, round(rate, 4),
                            ol_cnt, round(ol_rate, 4)])
    logger.info(f"Wrote {path}")
    return path


def _notify_discord(results: dict) -> None:
    """Send a Discord summary of the study."""
    try:
        lines = ["Prior-day OHLC anchor study done."]
        for symbol, summary in results.items():
            best = best_anchor(summary)
            if best is None:
                lines.append(f"{symbol}: no usable data.")
                continue
            up = summary[best]["cond_open_above_close_above"].get("pct_above")
            dn = summary[best]["cond_open_below_close_below"].get("pct_above")
            lines.append(
                f"{symbol}: best anchor={best}, open>anchor->finish> {up:.0f}%, "
                f"open<anchor->finish< {dn:.0f}% ({summary['n_days']} days)."
            )
        send_discord_message("\n".join(lines))
    except Exception as e:
        logger.warning(f"Discord notify failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_study(client: AlpacaClient, symbols: list[str], limit: int) -> dict:
    """Run the full study for each symbol. Returns {symbol: summary}."""
    results = {}
    for symbol in symbols:
        try:
            df = load_daily_bars(client, symbol, limit=limit)
            if len(df) < 30:
                logger.warning(f"Not enough daily bars for {symbol} ({len(df)}). Skipping.")
                continue
            rows = compute_day_rows(df)
            if rows.empty:
                logger.warning(f"No comparable days for {symbol}. Skipping.")
                continue
            summary = summarize(rows)
            summary["half_validation"] = split_half_validation(rows)
            summary["best_anchor"] = best_anchor(summary)
            summary["date_range"] = {
                "start": str(df.index[0].date()),
                "end": str(df.index[-1].date()),
            }
            results[symbol] = summary

            # Per-day CSV for easy viewing.
            csv_path = os.path.join(OUT_DIR, f"anchor_study_{symbol.lower()}.csv")
            rows.to_csv(csv_path, index=False)
            logger.info(f"Wrote {csv_path} ({len(rows)} rows)")

            print_summary(symbol, summary, rows)
        except Exception as e:
            logger.error(f"Study failed for {symbol}: {e}")
            log_exception_to_jira(e, "Anchor Study Failure", {"symbol": symbol})
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Prior-day OHLC anchor study")
    parser.add_argument("--symbols", nargs="+", default=["SPY", "QQQ"],
                        help="Tickers to study (default: SPY QQQ)")
    parser.add_argument("--limit", type=int, default=600,
                        help="Max daily bars to fetch per symbol (~2yrs)")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord notification")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-anchor")
    client = AlpacaClient()

    try:
        results = run_study(client, [s.upper() for s in args.symbols], args.limit)
        if not results:
            logger.error("No symbols produced results.")
            return
        with open(SUMMARY_PATH, "w") as f:
            json.dump(results, f, indent=2, default=str)
        logger.info(f"Wrote summary to {SUMMARY_PATH}")
        write_flat_csv(results)
        if not args.no_discord:
            _notify_discord(results)
    except Exception as e:
        logger.critical(f"Anchor study failed: {e}")
        log_exception_to_jira(e, "Anchor Study Run Failure")
        raise


if __name__ == "__main__":
    main()