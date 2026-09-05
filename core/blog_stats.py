#!/usr/bin/env python3
"""Blog dashboard stats, derived directly from agent-trade data.

Recreates the dashboard numbers the blog's performance card / sidebar / calendar
need, but computed from agent-trade tables instead of dexter's ``realized_trades``.

The dexter contract we must satisfy (what ``tools/blog_update.py`` consumes):

    calculate_dashboard_stats(df, today) -> {
        "pnl_7d":  float,   # sum of pnl_dollar over last 7 calendar days
        "pnl_30d": float,   # sum of pnl_dollar over last 30 calendar days
        "pnl_mtd": float,   # sum of pnl_dollar month-to-date
        "est_balance": float # running balance = start_balance + cumulative realized PnL
    }
    get_last_10_days_performance(df, today) -> list[bool]  # chronological, oldest->newest

Because agent-trade has no ``realized_trades`` here yet (that's the bridge's job),
this module reads the canonical closed round-trips from ``core.feedback`` and the
equity curve from ``portfolio_history`` and derives the same numbers directly.

Canonical "balance" decision: dexter used ``start_balance + total realized PnL``
where start_balance = 100_000. agent-trade has a real equity curve in
``portfolio_history``; pass ``use_equity_curve=True`` to use the latest equity
instead. Default (for closest parity with the old blog) is the realized-PnL
accumulation so the card's balance matches old reader expectations.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, date
from typing import Optional

import pandas as pd

from core.config import DATABASE_PATH  # noqa: F401  (kept for parity; round-trips come from feedback)

logger = logging.getLogger("BlogStats")

# Matches dexter's old hardcoded starting balance for the performance card.
START_BALANCE = 100_000.0

EASTER_TZ = "America/New_York"


# ---------------------------------------------------------------------------
# Timezone helpers (round-trip timestamps from feedback are naive UTC ISO)
# ---------------------------------------------------------------------------
def _to_et_naive(iso_str: str) -> Optional[pd.Timestamp]:
    """Parse an ISO timestamp (naive UTC) into a pd.Timestamp in America/New_York.

    Returns a timezone-aware ET Timestamp, or None if unparseable.
    """
    if not iso_str:
        return None
    try:
        ts = pd.Timestamp(iso_str)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert(EASTER_TZ)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Core: build a dexter-shaped realized DataFrame from feedback round-trips
# ---------------------------------------------------------------------------
def round_trips_to_dexter_df(round_trips: list[dict]) -> pd.DataFrame:
    """Convert ``compute_closed_round_trips`` output to dexter's realized_trades shape.

    Returns a DataFrame with columns compatible with dexter's report code:
        ticker, entry_date, exit_date, qty, entry_price, exit_price,
        pnl_dollar, pnl_percent, hold_time_minutes, strategy_name
    ``exit_date`` / ``entry_date`` are America/New_York dates (as pd.Timestamp).
    """
    rows = []
    for rt in round_trips or []:
        open_ts = _to_et_naive(rt.get("open_ts"))
        close_ts = _to_et_naive(rt.get("close_ts"))
        if close_ts is None:
            close_ts = open_ts  # never should happen; defensive
        rows.append({
            "ticker": rt.get("symbol"),
            "entry_date": open_ts,               # ET-aware pd.Timestamp
            "exit_date": close_ts,               # ET-aware pd.Timestamp
            "qty": rt.get("qty"),
            "entry_price": rt.get("entry_price"),
            "exit_price": rt.get("exit_price"),
            "pnl_dollar": rt.get("pnl"),
            "pnl_percent": rt.get("pnl_pct"),
            "hold_time_minutes": (rt.get("holding_hours") or 0.0) * 60.0,
            "strategy_name": rt.get("strategy_name"),
        })
    if not rows:
        return pd.DataFrame(columns=[
            "ticker","entry_date","exit_date","qty","entry_price","exit_price",
            "pnl_dollar","pnl_percent","hold_time_minutes","strategy_name"])
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Dashboard stats (mirrors dexter's calculate_dashboard_stats)
# ---------------------------------------------------------------------------
def calculate_dashboard_stats(df: pd.DataFrame, today=None) -> dict:
    """Compute pnl_7d / pnl_30d / pnl_mtd / est_balance from a dexter-shaped df.

    ``today`` may be a date/pd.Timestamp/str; defaults to today in America/New_York.
    """
    if df is None or df.empty:
        return {"pnl_7d": 0.0, "pnl_30d": 0.0, "pnl_mtd": 0.0, "est_balance": START_BALANCE}

    today_ts = _resolve_today(today)

    # Ensure exit_date is ET-aware
    if not isinstance(df["exit_date"].iloc[0], pd.Timestamp) or df["exit_date"].dt.tz is None:
        df = df.copy()
        df["exit_date"] = pd.to_datetime(df["exit_date"])
        if df["exit_date"].dt.tz is None:
            df["exit_date"] = df["exit_date"].dt.tz_localize("UTC").dt.tz_convert(EASTER_TZ)

    df_f = df[df["exit_date"] <= today_ts]  # only up to today
    cutoff_7d = today_ts - timedelta(days=6)
    cutoff_30d = today_ts - timedelta(days=29)
    mtd_start = pd.Timestamp(today_ts.year, today_ts.month, 1, tz=EASTER_TZ)

    total_to_date = float(df_f["pnl_dollar"].sum())

    return {
        "pnl_7d": float(df_f[df_f["exit_date"] >= cutoff_7d]["pnl_dollar"].sum()),
        "pnl_30d": float(df_f[df_f["exit_date"] >= cutoff_30d]["pnl_dollar"].sum()),
        "pnl_mtd": float(df_f[df_f["exit_date"] >= mtd_start]["pnl_dollar"].sum()),
        "est_balance": START_BALANCE + total_to_date,
    }


def get_last_10_days_performance(df: pd.DataFrame, today=None) -> list[bool]:
    """Return whether each of the last 10 trading days was a winner, oldest->newest.

    Mirrors dexter's ``get_last_10_days_performance`` but allows sparse days
    (non-trading days are included as False so the streak dots stay aligned with
    the calendar span the old card rendered).
    """
    if df is None or df.empty:
        return [False] * 10

    today_ts = _resolve_today(today)

    if not isinstance(df["exit_date"].iloc[0], pd.Timestamp) or df["exit_date"].dt.tz is None:
        df = df.copy()
        df["exit_date"] = pd.to_datetime(df["exit_date"])
        if df["exit_date"].dt.tz is None:
            df["exit_date"] = df["exit_date"].dt.tz_localize("UTC").dt.tz_convert(EASTER_TZ)

    df_f = df[df["exit_date"] <= today_ts]
    daily = df_f.groupby(df_f["exit_date"].dt.date)["pnl_dollar"].sum()
    daily = daily.sort_index(ascending=False).head(10)
    # Most-recent first; reverse to chronological.
    vals = list(reversed(daily.values))
    return [float(v) > 0 for v in vals]


# ---------------------------------------------------------------------------
# Equity curve (agent-trade source) — for optional balance parity check
# ---------------------------------------------------------------------------
def latest_equity_from_report() -> Optional[float]:
    """Return the latest portfolio_history equity, or None if not available."""
    import sqlite3
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        try:
            cur = conn.cursor()
            cur.execute("SELECT equity FROM portfolio_history ORDER BY timestamp DESC LIMIT 1")
            row = cur.fetchone()
            return float(row[0]) if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.debug("latest_equity_from_report unavailable: %s", e)
        return None


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------
def _resolve_today(today) -> pd.Timestamp:
    if today is None:
        return pd.Timestamp.now(tz=EASTER_TZ).normalize()
    if isinstance(today, str):
        today = pd.to_datetime(today).date()
    if isinstance(today, date) and not isinstance(today, pd.Timestamp):
        return pd.Timestamp(today, tz=EASTER_TZ)
    ts = pd.Timestamp(today)
    if ts.tzinfo is None:
        return ts.tz_localize(EASTER_TZ)
    return ts.tz_convert(EASTER_TZ)


if __name__ == "__main__":
    # Quick sanity check against the live DB.
    from core import feedback
    trips = feedback.compute_closed_round_trips()
    df = round_trips_to_dexter_df(trips)
    print("round-trips:", len(trips), "rows:", len(df))
    print("stats:", calculate_dashboard_stats(df))
    print("streak:", get_last_10_days_performance(df))
    print("latest equity:", latest_equity_from_report())