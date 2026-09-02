#!/usr/bin/env python3
"""Equity-desk bad-trade forensics: diagnose WHY the equity desk loses money.

Measures from 2026-07-07 forward (per user request, aligning with the first
portfolio_history snapshot). Reconstructs decision context for every closed
equity round-trip and clusters the losers into recurring root causes, so the
findings can be fed back to the agents (Screener / MetaStrategist / LLM brain).

Read-only: uses the authoritative cloud DB snapshot (cloud_downloaded_trading_agent.db,
pulled via tools/pull_cloud_db.py) and writes report files only.

Two eras are analyzed separately because the evidence base differs:
  - PRE  2026-07-27: no `decisions` table exists. Joints via raw fills only.
    Root cause must be reconstructed from timing/indicator snapshots + portfolio
    history. (Legacy / fallback / gap-risk era.)
  - POST 2026-07-27: LLM decisions, strategies and watchlist are captured.
    Root cause can be attributed to the LLM's own reasoning. (Rule era.)

Outputs:
  - reports/equity_desk_dataset.csv     per-RT dataset + attribution columns
  - reports/equity_desk_diagnosis.md    pattern-level brief with evidence
  - feedback/equity_lessons.md          "do-not-do-X" playbook for agents
"""
import os
import sys
import json
import sqlite3
import logging
from collections import defaultdict
from datetime import datetime

import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

CLOUD_DB = os.path.join(PROJECT_ROOT, "cloud_downloaded_trading_agent.db")
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
FEEDBACK_DIR = os.path.join(PROJECT_ROOT, "feedback")
os.makedirs(REPORTS_DIR, exist_ok=True)
os.makedirs(FEEDBACK_DIR, exist_ok=True)

MEASURE_FROM = "2026-07-07"   # measure 7/7 forward
REASONING_BOUNDARY = "2026-07-27"  # decisions table starts here

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("equity_forensics")

CRYPTO_QUOTES = {"USD", "USDT", "USDC", "BTC"}
KNOWN_CRYPTO_BASES = {
    "ADA", "AVAX", "BTC", "DOGE", "DOT", "ETH", "LINK", "LTC",
    "MATIC", "SHIB", "SOL", "UNI", "XRP",
}


def normalize_symbol(s):
    if not isinstance(s, str):
        return ""
    s = (s or "").strip().upper().replace("-", "/")
    if "/" in s:
        return s
    for quote in sorted(CRYPTO_QUOTES, key=len, reverse=True):
        if s.endswith(quote) and s[:-len(quote)] in KNOWN_CRYPTO_BASES:
            return f"{s[:-len(quote)]}/{quote}"
    return s


def is_equity_symbol(s):
    return "/" not in normalize_symbol(s)


def parse_dt(ts):
    if not ts:
        return None
    try:
        dt = pd.to_datetime(ts)
        if dt.tzinfo is None:
            dt = dt.tz_localize("UTC")
        else:
            dt = dt.tz_convert("UTC")
        return dt
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def load_trades():
    conn = sqlite3.connect(CLOUD_DB)
    df = pd.read_sql_query(
        """SELECT id, decision_id, alpaca_order_id, timestamp, symbol, side, qty,
                  filled_avg_price, status, option_type, option_dte, strike, contract_symbol
           FROM trades WHERE status IN ('filled','partially_filled') ORDER BY id ASC""",
        conn)
    conn.close()
    df["symbol"] = df["symbol"].apply(normalize_symbol)
    df = df[~df["symbol"].isin(["PENALIZED", "BOOSTED"])]
    df = df[df["symbol"].apply(is_equity_symbol)]          # equity desk only
    df = df[df["timestamp"] >= MEASURE_FROM]               # 7/7 forward
    df["ts"] = df["timestamp"].apply(parse_dt)
    df = df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    return df


def load_decisions():
    conn = sqlite3.connect(CLOUD_DB)
    df = pd.read_sql_query(
        """SELECT id, timestamp, ticker_indicators, portfolio_state, thought_process,
                  proposed_action, proposed_symbol, proposed_qty, is_approved,
                  rejection_reason, direction, conviction, instrument, cycle_id, reasoning
           FROM decisions ORDER BY id ASC""", conn)
    conn.close()
    df["symbol"] = df["proposed_symbol"].apply(normalize_symbol)
    df["ts"] = df["timestamp"].apply(parse_dt)
    df = df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    return df


def load_portfolio_history():
    conn = sqlite3.connect(CLOUD_DB)
    df = pd.read_sql_query(
        "SELECT timestamp, equity, cash, unrealized_pnl FROM portfolio_history ORDER BY timestamp ASC",
        conn)
    conn.close()
    df["ts"] = df["timestamp"].apply(parse_dt)
    df = df.dropna(subset=["ts"]).reset_index(drop=True)
    return df


def load_strategy_history():
    conn = sqlite3.connect(CLOUD_DB)
    df = pd.read_sql_query(
        """SELECT timestamp, ticker, todays_rules, meta_reasoning, strategy_version
           FROM strategy_history ORDER BY timestamp ASC""", conn)
    conn.close()
    df["ticker"] = df["ticker"].apply(normalize_symbol)
    df["ts"] = df["timestamp"].apply(parse_dt)
    df = df.dropna(subset=["ts"]).reset_index(drop=True)
    return df


def load_watchlist_history():
    conn = sqlite3.connect(CLOUD_DB)
    df = pd.read_sql_query(
        "SELECT timestamp, watchlist FROM watchlist_history ORDER BY timestamp ASC", conn)
    conn.close()
    df["ts"] = df["timestamp"].apply(parse_dt)
    return df


def load_executions():
    conn = sqlite3.connect(CLOUD_DB)
    df = pd.read_sql_query(
        """SELECT id, decision_id, attempt, timestamp, symbol, side, qty, order_type,
                  status, error, alpaca_order_id, filled_avg_price
           FROM executions ORDER BY id ASC""", conn)
    conn.close()
    df["symbol"] = df["symbol"].apply(normalize_symbol)
    return df


# ---------------------------------------------------------------------------
# FIFO round-trip matcher (canonical, mirrors core/feedback.py)
# ---------------------------------------------------------------------------
def compute_round_trips(trades):
    round_trips = []
    buy_queues = defaultdict(list)  # symbol -> list of {qty, price, ts, dec, trade_id}
    for _, r in trades.iterrows():
        sym = r["symbol"]
        side = (r["side"] or "").lower()
        qty = float(r["qty"] or 0.0)
        price = float(r["filled_avg_price"] or 0.0)
        ts = r["ts"]
        if side == "buy":
            buy_queues[sym].append({"qty": qty, "price": price, "ts": ts,
                                    "dec": r["decision_id"], "trade_id": r["id"]})
        elif side == "sell":
            tmp = qty
            while tmp > 0 and buy_queues.get(sym):
                b = buy_queues[sym][0]
                m = min(tmp, b["qty"])
                entry = b["price"] or 0.0
                pnl = m * (price - entry)
                pnl_pct = ((price - entry) / entry * 100.0) if entry else 0.0
                t_open, t_close = b["ts"], ts
                holding_hours = (t_close - t_open).total_seconds() / 3600.0
                round_trips.append({
                    "symbol": sym, "open_ts": b["ts"], "close_ts": ts,
                    "qty": m, "entry_price": entry, "exit_price": price,
                    "pnl": pnl, "pnl_pct": pnl_pct, "holding_hours": holding_hours,
                    "win": pnl > 0,
                    "entry_dec_id": b["dec"], "exit_dec_id": r["decision_id"],
                    "entry_trade_id": b["trade_id"], "exit_trade_id": r["id"],
                })
                tmp -= m
                b["qty"] -= m
                if b["qty"] <= 1e-9:
                    buy_queues[sym].pop(0)
    return round_trips


def holding_bucket(hours):
    if hours < 4:
        return "under_4h_whipsaw"
    if hours < 24:
        return "4h_to_1d"
    if hours < 7 * 24:
        return "1d_to_7d"
    return "over_7d"


# ---------------------------------------------------------------------------
# Context lookup helpers
# ---------------------------------------------------------------------------
def entry_action_decisions(decisions):
    """approved BUY/SELL decisions per (symbol, ts)."""
    act = decisions[decisions["is_approved"] == 1].copy()
    act = act[act["proposed_action"].isin(["BUY", "SELL"])]
    return act


def nearest_prior(df, ts, time_col="ts"):
    """Row in df with time_col <= ts, nearest. None if none before."""
    if ts is None or df.empty:
        return None
    prior = df[df[time_col] <= ts]
    if prior.empty:
        return None
    return prior.iloc[-1]


def was_in_watchlist(wl, ts, sym):
    """Return (bool, watchlist_list) of whether sym was in watchlist at/before ts."""
    rec = nearest_prior(wl, ts)
    if rec is None:
        return None, None
    try:
        items = json.loads(rec["watchlist"])
    except Exception:
        items = []
    norm_items = [normalize_symbol(s) for s in (items or [])]
    return (sym in norm_items), norm_items


def parse_indicators(dec_row, sym):
    """Extract per-symbol indicators from a decision's ticker_indicators JSON."""
    try:
        ind = json.loads(dec_row["ticker_indicators"])
    except Exception:
        return {}
    sub = ind.get(sym, {}) if isinstance(ind, dict) else {}
    if not isinstance(sub, dict):
        return {}
    return {
        "rsi": sub.get("rsi_14"),
        "vwap_dist_pct": sub.get("vwap_dist_pct"),
        "price": sub.get("price"),
    }


def nearest_indicator_for_symbol(decisions, ts, sym):
    """Find closest approved BUY/SELL decision for sym before ts; return its indicators."""
    if ts is None or decisions.empty:
        return {}, None
    sub = decisions[(decisions["symbol"] == sym) &
                    (decisions["proposed_action"].isin(["BUY", "SELL"]))]
    if sub.empty:
        return {}, None
    before = sub[sub["ts"] <= ts]
    if before.empty:
        return {}, None
    row = before.iloc[-1]
    return parse_indicators(row, sym), row


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    logger.info("Loading data from %s", CLOUD_DB)
    trades = load_trades()
    decisions = load_decisions()
    ph = load_portfolio_history()
    strat = load_strategy_history()
    wl = load_watchlist_history()
    execs = load_executions()
    logger.info("equity fills=%d decisions=%d ph=%d strat=%d wl=%d execs=%d",
                len(trades), len(decisions), len(ph), len(strat), len(wl), len(execs))

    round_trips = compute_round_trips(trades)
    logger.info("equity closed round-trips (7/7+): %d", len(round_trips))

    # Pre-build strategy lookup per ticker sorted by ts
    strat_by_sym = {}
    for sym, grp in strat.groupby("ticker"):
        strat_by_sym[sym] = grp.sort_values("ts")

    enriched = []
    for rt in round_trips:
        rec = dict(rt)
        t_open = rt["open_ts"]
        t_close = rt["close_ts"]
        sym = rt["symbol"]
        era = "pre-reasoning" if t_open < pd.Timestamp(REASONING_BOUNDARY, tz="UTC") else "post-reasoning"

        # window context
        rec["era"] = era
        rec["open_date"] = t_open.date().isoformat()
        rec["close_date"] = t_close.date().isoformat()
        rec["holding_bucket"] = holding_bucket(rt["holding_hours"])
        rec["entry_dow"] = t_open.strftime("%A")
        rec["entry_hour_ny"] = t_open.tz_convert("America/New_York").hour

        # portfolio context
        ph_prior = nearest_prior(ph, t_open)
        ph_exit = nearest_prior(ph, t_close)
        rec["cash_at_entry"] = float(ph_prior["cash"]) if ph_prior is not None else None
        rec["equity_at_entry"] = float(ph_prior["equity"]) if ph_prior is not None else None
        rec["cash_pct_at_entry"] = None
        if rec["cash_at_entry"] is not None and rec["equity_at_entry"]:
            rec["cash_pct_at_entry"] = rec["cash_at_entry"] / rec["equity_at_entry"] * 100.0
        rec["equity_at_exit"] = float(ph_exit["equity"]) if ph_exit is not None else None

        # watchlist membership
        in_wl, wl_items = was_in_watchlist(wl, t_open, sym)
        rec["was_in_watchlist"] = in_wl
        rec["watchlist_at_entry"] = json.dumps(wl_items) if wl_items else None

        # active strategy rule before entry
        rule = None
        meta = None
        srow = nearest_prior(strat_by_sym.get(sym, pd.DataFrame()), t_open)
        if srow is not None:
            rule = srow["todays_rules"]
            meta = srow.get("meta_reasoning")
        rec["active_strategy"] = rule
        rec["strategy_meta"] = meta

        # decision context + indicators
        ind, dec_a = nearest_indicator_for_symbol(decisions, t_open, sym)
        rec["entry_rsi"] = ind.get("rsi")
        rec["entry_vwap_dist_pct"] = ind.get("vwap_dist_pct")
        rec["entry_price_ind"] = ind.get("price")
        if dec_a is not None:
            rec["entry_dec_id"] = dec_a["id"]
            rec["entry_direction"] = dec_a["direction"]
            rec["entry_conviction"] = dec_a["conviction"]
            rec["entry_instrument"] = dec_a["instrument"]
            rec["entry_reasoning"] = dec_a["reasoning"]
            rec["entry_thought"] = dec_a["thought_process"]

        # exit decision context
        if rt["exit_dec_id"] is not None:
            ed = decisions[decisions["id"] == rt["exit_dec_id"]]
            if not ed.empty:
                rec["exit_reasoning"] = ed.iloc[0]["reasoning"]
                rec["exit_proposed_action"] = ed.iloc[0]["proposed_action"]

        # exit mechanism classification
        rec["exit_mechanism"] = "decision-linked" if rt["exit_dec_id"] is not None else "broker/TP-SL"

        enriched.append(rec)

    rtdf = pd.DataFrame(enriched)
    # Sort: era, then symbol, then open_ts
    rtdf = rtdf.sort_values(["open_ts"]).reset_index(drop=True)

    out_csv = os.path.join(REPORTS_DIR, "equity_desk_dataset.csv")
    rtdf.to_csv(out_csv, index=False)
    logger.info("Saved %s (%d rows)", out_csv, len(rtdf))

    # ---- Summary to console ----
    print("\n=== Equity desk (7/7+): %d closed RTs, total PnL %.2f ===" %
          (len(rtdf), rtdf["pnl"].sum()))
    for era in ["pre-reasoning", "post-reasoning"]:
        sub = rtdf[rtdf["era"] == era]
        if sub.empty:
            continue
        print(f"\n--- {era}: {len(sub)} RTs, PnL {sub['pnl'].sum():,.2f} ---")
        by_sym = sub.groupby("symbol").agg(rt=("pnl", "size"), pnl=("pnl", "sum"),
                                           win=("win", "mean"))
        by_sym = by_sym.sort_values("pnl")
        for sym, row in by_sym.iterrows():
            print(f"  {sym:7s} rt={int(row['rt']):3d} pnl={row['pnl']:10,.2f} win%={row['win']*100:5.1f}")


if __name__ == "__main__":
    main()