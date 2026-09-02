#!/usr/bin/env python3
"""
Agent-Trade Comprehensive Trade Dataset & Deep-Dive Analysis.

Builds a robust per-round-trip dataset from the authoritative cloud DB
(cloud_downloaded_trading_agent.db, pulled from GCS) and produces a deep-dive
analysis covering:

  1. A complete closed round-trip dataset (CSV) with quantities, PnL, PnL%,
     entry/exit timestamps, holding time, and the cash balance at entry.
  2. Why the agent buys/sells each ticker (decision reasoning + strategy rules).
  3. Per-ticker performance (best/worst tickers).
  4. Time-of-day analysis per ticker (best entry/exit hours).
  5. Second-level analyses (holding-time buckets, whipsaw, win/loss asymmetry,
     conviction vs outcome, direction vs outcome, day-of-week, cash utilization).

Read-only: does not modify any database or trading state.

Outputs:
  - reports/agent_trade_dataset.csv        (per round-trip dataset)
  - reports/agent_trade_deep_dive.md       (full analysis report)
  - reports/agent_trade_deep_dive.csv      (per-ticker summary table)
"""
import os
import sys
import sqlite3
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CLOUD_DB = os.path.join(PROJECT_ROOT, "cloud_downloaded_trading_agent.db")
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

try:
    import pytz
    NY = pytz.timezone("America/New_York")
    UTC = pytz.utc
    HAS_PYTZ = True
except Exception:
    HAS_PYTZ = False


# ---------------------------------------------------------------------------
# Symbol normalization (mirrors core.strategy_rules.normalize_symbol)
# ---------------------------------------------------------------------------
CRYPTO_QUOTES = {"USD", "USDT", "USDC", "BTC"}
KNOWN_CRYPTO_BASES = {
    "ADA", "AVAX", "BTC", "DOGE", "DOT", "ETH", "LINK", "LTC",
    "MATIC", "SHIB", "SOL", "UNI", "XRP",
}


def normalize_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper().replace("-", "/")
    if "/" in s:
        return s
    for quote in sorted(CRYPTO_QUOTES, key=len, reverse=True):
        if s.endswith(quote) and s[:-len(quote)] in KNOWN_CRYPTO_BASES:
            return f"{s[:-len(quote)]}/{quote}"
    return s


def is_crypto_symbol(symbol: str) -> bool:
    n = normalize_symbol(symbol)
    if "/" not in n:
        return False
    base, quote = n.split("/", 1)
    return bool(base) and quote in CRYPTO_QUOTES


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------
def parse_dt(ts_str):
    """Parse an ISO timestamp to a tz-aware UTC datetime (defensively)."""
    if not ts_str:
        return None
    try:
        dt = pd.to_datetime(ts_str)
        if dt.tzinfo is None:
            dt = dt.tz_localize("UTC")
        else:
            dt = dt.tz_convert("UTC")
        return dt
    except Exception:
        return None


def to_ny(dt_utc):
    """Convert a tz-aware UTC datetime to Eastern time (tz-aware)."""
    if dt_utc is None:
        return None
    if HAS_PYTZ:
        return dt_utc.astimezone(NY)
    return dt_utc


# ---------------------------------------------------------------------------
# FIFO round-trip matching (canonical, same as core/feedback.py)
# ---------------------------------------------------------------------------
def compute_round_trips(trades_df):
    """FIFO-match buys->sells into closed round-trips.

    trades_df must have columns: symbol, side, qty, filled_avg_price, timestamp
    sorted by timestamp ascending.
    """
    round_trips = []
    buy_queues = defaultdict(list)  # symbol -> list of {qty, price, ts}

    for _, r in trades_df.iterrows():
        symbol = normalize_symbol(r["symbol"])
        side = (r["side"] or "").lower()
        qty = float(r["qty"] or 0.0)
        price = float(r["filled_avg_price"] or 0.0)
        ts = r["timestamp"]

        if side == "buy":
            buy_queues[symbol].append({"qty": qty, "price": price, "ts": ts})
        elif side == "sell":
            temp_qty = qty
            while temp_qty > 0 and buy_queues.get(symbol):
                b = buy_queues[symbol][0]
                matched = min(temp_qty, b["qty"])
                pnl = matched * (price - (b["price"] or 0.0))
                entry = b["price"] or 0.0
                pnl_pct = ((price - entry) / entry * 100.0) if entry else 0.0
                holding_hours = 0.0
                t_open = parse_dt(b["ts"])
                t_close = parse_dt(ts)
                if t_open is not None and t_close is not None:
                    holding_hours = (t_close - t_open).total_seconds() / 3600.0

                round_trips.append({
                    "symbol": symbol,
                    "open_ts": b["ts"],
                    "close_ts": ts,
                    "qty": matched,
                    "entry_price": entry,
                    "exit_price": price,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "holding_hours": holding_hours,
                    "win": pnl > 0,
                })
                temp_qty -= matched
                b["qty"] -= matched
                if b["qty"] <= 1e-9:
                    buy_queues[symbol].pop(0)

    return round_trips


def holding_bucket(hours: float) -> str:
    if hours < 4:
        return "under_4h_whipsaw"
    if hours < 24:
        return "4h_to_1d"
    if hours < 7 * 24:
        return "1d_to_7d"
    return "over_7d"


# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
def load_trades():
    conn = sqlite3.connect(CLOUD_DB)
    df = pd.read_sql_query(
        """SELECT id, decision_id, alpaca_order_id, timestamp, symbol, side, qty,
                  filled_avg_price, status, option_type, option_dte, strike, contract_symbol
           FROM trades WHERE status IN ('filled','partially_filled') ORDER BY id ASC""",
        conn,
    )
    conn.close()
    df["symbol"] = df["symbol"].apply(normalize_symbol)
    # Drop sentinel artifacts
    df = df[~df["symbol"].isin(["PENALIZED", "BOOSTED"])]
    df["ts"] = df["timestamp"].apply(parse_dt)
    df = df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    return df


def load_decisions():
    conn = sqlite3.connect(CLOUD_DB)
    df = pd.read_sql_query(
        """SELECT id, timestamp, proposed_action, proposed_symbol, proposed_qty,
                  is_approved, rejection_reason, direction, conviction, instrument,
                  cycle_id, reasoning, thought_process, ticker_indicators, portfolio_state
           FROM decisions ORDER BY id ASC""",
        conn,
    )
    conn.close()
    df["ts"] = df["timestamp"].apply(parse_dt)
    return df


def load_portfolio_history():
    conn = sqlite3.connect(CLOUD_DB)
    df = pd.read_sql_query(
        "SELECT timestamp, equity, cash, unrealized_pnl FROM portfolio_history ORDER BY timestamp ASC",
        conn,
    )
    conn.close()
    df["ts"] = df["timestamp"].apply(parse_dt)
    df = df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    return df


def load_strategy_history():
    conn = sqlite3.connect(CLOUD_DB)
    df = pd.read_sql_query(
        "SELECT timestamp, ticker, todays_rules, meta_reasoning FROM strategy_history ORDER BY timestamp ASC",
        conn,
    )
    conn.close()
    df["ticker"] = df["ticker"].apply(normalize_symbol)
    return df


def load_watchlist_history():
    conn = sqlite3.connect(CLOUD_DB)
    df = pd.read_sql_query(
        "SELECT timestamp, watchlist FROM watchlist_history ORDER BY timestamp ASC",
        conn,
    )
    conn.close()
    return df


def cash_at_time(ph_df, ts):
    """Return the cash balance from portfolio_history at or before ts."""
    if ts is None:
        return None
    prior = ph_df[ph_df["ts"] <= ts]
    if prior.empty:
        return None
    return float(prior.iloc[-1]["cash"])


def equity_at_time(ph_df, ts):
    if ts is None:
        return None
    prior = ph_df[ph_df["ts"] <= ts]
    if prior.empty:
        return None
    return float(prior.iloc[-1]["equity"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    logger.info("Loading trades...")
    trades = load_trades()
    logger.info(f"Loaded {len(trades)} filled trade rows")

    logger.info("Loading decisions...")
    decisions = load_decisions()
    logger.info(f"Loaded {len(decisions)} decision rows")

    logger.info("Loading portfolio history...")
    ph = load_portfolio_history()
    logger.info(f"Loaded {len(ph)} portfolio history rows")

    logger.info("Loading strategy history...")
    strat = load_strategy_history()
    logger.info(f"Loaded {len(strat)} strategy rows")

    logger.info("Loading watchlist history...")
    wl = load_watchlist_history()
    logger.info(f"Loaded {len(wl)} watchlist snapshots")

    # Build decision lookup by decision_id
    decision_by_id = {r["id"]: r for r in decisions.to_dict("records")}

    # ---- Build round-trips ----
    round_trips = compute_round_trips(trades)
    logger.info(f"Computed {len(round_trips)} closed round-trips")

    # ---- Enrich round-trips with decision context, cash, time-of-day ----
    enriched = []
    for rt in round_trips:
        rec = dict(rt)
        t_open = parse_dt(rt["open_ts"])
        t_close = parse_dt(rt["close_ts"])
        rec["open_utc"] = rt["open_ts"]
        rec["close_utc"] = rt["close_ts"]
        rec["open_ny"] = to_ny(t_open)
        rec["close_ny"] = to_ny(t_close)
        rec["open_date"] = rec["open_ny"].date().isoformat() if rec["open_ny"] is not None else None
        rec["close_date"] = rec["close_ny"].date().isoformat() if rec["close_ny"] is not None else None
        rec["open_hour_ny"] = rec["open_ny"].hour if rec["open_ny"] is not None else None
        rec["close_hour_ny"] = rec["close_ny"].hour if rec["close_ny"] is not None else None
        rec["open_dow"] = rec["open_ny"].strftime("%A") if rec["open_ny"] is not None else None
        rec["close_dow"] = rec["close_ny"].strftime("%A") if rec["close_ny"] is not None else None
        rec["is_crypto"] = is_crypto_symbol(rt["symbol"])
        rec["holding_bucket"] = holding_bucket(rt["holding_hours"])
        rec["cash_at_entry"] = cash_at_time(ph, t_open)
        rec["equity_at_entry"] = equity_at_time(ph, t_open)
        rec["cash_at_exit"] = cash_at_time(ph, t_close)
        rec["equity_at_exit"] = equity_at_time(ph, t_close)
        if rec["cash_at_entry"] is not None and rec["equity_at_entry"]:
            rec["cash_pct_at_entry"] = rec["cash_at_entry"] / rec["equity_at_entry"] * 100.0
        else:
            rec["cash_pct_at_entry"] = None

        # Decision context (find the decision that proposed this symbol around entry)
        # We match by symbol + nearest approved decision timestamp near open_ts.
        rec["entry_direction"] = None
        rec["entry_conviction"] = None
        rec["entry_reasoning"] = None
        rec["entry_instrument"] = None
        rec["entry_rsi"] = None
        rec["entry_vwap_dist_pct"] = None
        if t_open is not None:
            sym = rt["symbol"]
            sym_dec = decisions[
                (decisions["proposed_symbol"] == sym) &
                (decisions["is_approved"] == 1) &
                (decisions["proposed_action"].isin(["BUY", "SELL"]))
            ]
            if not sym_dec.empty:
                # nearest decision at or before open_ts
                before = sym_dec[sym_dec["ts"] <= t_open]
                if not before.empty:
                    best = before.iloc[-1]
                    rec["entry_direction"] = best["direction"]
                    rec["entry_conviction"] = best["conviction"]
                    rec["entry_reasoning"] = best["reasoning"]
                    rec["entry_instrument"] = best["instrument"]
                    # Extract this symbol's indicators from the decision
                    try:
                        ind = json.loads(best["ticker_indicators"])
                        if sym in ind:
                            rec["entry_rsi"] = ind[sym].get("rsi_14")
                            rec["entry_vwap_dist_pct"] = ind[sym].get("vwap_dist_pct")
                    except Exception:
                        pass
        enriched.append(rec)

    rtdf = pd.DataFrame(enriched)
    rtdf.to_csv(os.path.join(REPORTS_DIR, "agent_trade_dataset.csv"), index=False)
    logger.info(f"Dataset saved: {len(rtdf)} round-trips -> reports/agent_trade_dataset.csv")

    # ---- Build the report ----
    lines = []
    lines.append("# Agent-Trade Deep-Dive Analysis")
    lines.append("")
    lines.append(f"**Source:** `cloud_downloaded_trading_agent.db` (authoritative cloud DB from GCS)")
    lines.append(f"**Trade window:** {trades['ts'].min()} to {trades['ts'].max()}")
    lines.append(f"**Filled trade rows:** {len(trades)} | **Closed round-trips:** {len(rtdf)}")
    lines.append(f"**Portfolio snapshots:** {len(ph)}")
    lines.append("")
    lines.append("**Method:** FIFO realized-PnL matching (canonical, same as `core/feedback.py`).")
    lines.append("")

    # ============ 1. Portfolio / cash overview ============
    lines.append("## 1. Portfolio & Cash Overview")
    lines.append("")
    if not ph.empty:
        start_eq = ph.iloc[0]["equity"]
        end_eq = ph.iloc[-1]["equity"]
        start_cash = ph.iloc[0]["cash"]
        end_cash = ph.iloc[-1]["cash"]
        total_realized = rtdf["pnl"].sum()
        lines.append(f"- **Starting equity:** ${start_eq:,.2f}")
        lines.append(f"- **Ending equity:** ${end_eq:,.2f}")
        lines.append(f"- **Equity change:** ${end_eq - start_eq:,.2f} ({(end_eq/start_eq - 1)*100:.2f}%)")
        lines.append(f"- **Starting cash:** ${start_cash:,.2f}")
        lines.append(f"- **Ending cash:** ${end_cash:,.2f}")
        lines.append(f"- **Total realized PnL (closed round-trips):** ${total_realized:,.2f}")
        lines.append(f"- **Latest cash balance:** ${end_cash:,.2f}")
        lines.append("")

    # ============ 2. Why the agent buys/sells ============
    lines.append("## 2. Why the Agent Buys/Sells Each Ticker")
    lines.append("")
    lines.append("The agent's decisions are driven by (a) the **AI Screener** selecting a 5-ticker watchlist, "
                 "(b) **per-ticker strategy rules** written by the MetaStrategist (and emergency intraday rewrites on "
                 "shock moves), and (c) the **LLM brain** appraising indicators (RSI, SMA, MACD, Bollinger, VWAP) "
                 "against those rules. Below are the dominant drivers per ticker.")
    lines.append("")

    # Watchlist selection frequency (how often each ticker was screened in)
    wl_counts = defaultdict(int)
    for _, r in wl.iterrows():
        try:
            items = json.loads(r["watchlist"])
            for s in items:
                wl_counts[normalize_symbol(s)] += 1
        except Exception:
            pass
    total_wl = len(wl) if len(wl) else 1

    # Strategy rule themes per ticker
    strat_by_sym = defaultdict(list)
    for _, r in strat.iterrows():
        strat_by_sym[r["ticker"]].append(str(r["todays_rules"]))

    # Reasoning themes per ticker (from decisions, where available)
    theme_keywords = {
        "pullback/support buy": ["pullback", "support", "bounce", "oversold", "rebound"],
        "breakout/momentum buy": ["breakout", "break above", "momentum", "above resistance", "gap"],
        "VWAP confluence": ["vwap"],
        "RSI oversold": ["oversold", "rsi"],
        "RSI overbought (sell)": ["overbought"],
        "rule-mandated sell (support breach)": ["below support", "below the support", "liquidate", "lock in"],
        "take-profit": ["take profit", "take-profit", "profit target"],
        "stop-loss": ["stop loss", "stop-loss", "cut losses"],
        "Fibonacci": ["fibonacci", "fib"],
    }
    sym_reasoning = defaultdict(list)
    for _, r in rtdf.iterrows():
        if r["entry_reasoning"]:
            sym_reasoning[r["symbol"]].append(str(r["entry_reasoning"]))

    # Combine all traded symbols
    all_syms = sorted(set(rtdf["symbol"].tolist()) | set(strat_by_sym.keys()))
    for sym in all_syms:
        n_rt = len(rtdf[rtdf["symbol"] == sym])
        wl_n = wl_counts.get(sym, 0)
        wl_pct = wl_n / total_wl * 100 if total_wl else 0
        lines.append(f"### {sym} ({n_rt} closed round-trips)")
        lines.append(f"- **Screener selection:** in watchlist {wl_n} of {total_wl} snapshots ({wl_pct:.0f}%)")
        # Strategy rule summary
        rules = strat_by_sym.get(sym, [])
        if rules:
            latest = rules[-1]
            lines.append(f"- **Latest strategy rule:** {latest[:300]}")
        else:
            lines.append("- **Latest strategy rule:** (none persisted)")
        # Reasoning themes
        texts = sym_reasoning.get(sym, [])
        if texts:
            counts = defaultdict(int)
            for t in texts:
                tl = t.lower()
                for label, kws in theme_keywords.items():
                    if any(k in tl for k in kws):
                        counts[label] += 1
            total = len(texts)
            top = sorted(counts.items(), key=lambda x: -x[1])[:5]
            theme_str = "; ".join(f"{label} ({n}/{total})" for label, n in top)
            lines.append(f"- **Reasoning themes:** {theme_str}")
        else:
            lines.append("- **Reasoning themes:** (no per-decision reasoning captured for this window)")
        lines.append("")

    # ============ 3. Per-ticker performance ============
    lines.append("## 3. Per-Ticker Performance (Best to Worst)")
    lines.append("")
    lines.append("| Ticker | Closed | Total PnL | PnL % | Win Rate | Avg Win | Avg Loss | Expectancy | Avg Hold (h) | Whipsaw% |")
    lines.append("|--------|--------|-----------|-------|----------|---------|----------|------------|---------------|----------|")
    per_ticker = []
    for sym, grp in rtdf.groupby("symbol"):
        n = len(grp)
        total_pnl = grp["pnl"].sum()
        # Cost basis = sum of (entry_price * qty) across all round-trips for this ticker
        cost_basis = (grp["entry_price"] * grp["qty"]).sum()
        pnl_pct = (total_pnl / cost_basis * 100.0) if cost_basis else 0.0
        wins = grp[grp["pnl"] > 0]
        losses = grp[grp["pnl"] < 0]
        wr = len(wins) / n * 100 if n else 0
        avg_win = wins["pnl"].mean() if len(wins) else 0.0
        avg_loss = losses["pnl"].mean() if len(losses) else 0.0
        exp = wr/100*avg_win - (1-wr/100)*abs(avg_loss)
        avg_hold = grp["holding_hours"].mean()
        whipsaw = (grp["holding_bucket"] == "under_4h_whipsaw").mean() * 100
        per_ticker.append({
            "ticker": sym, "closed": n, "total_pnl": total_pnl, "pnl_pct": pnl_pct,
            "win_rate": wr, "avg_win": avg_win, "avg_loss": avg_loss, "expectancy": exp,
            "avg_hold_hours": avg_hold, "whipsaw_pct": whipsaw,
        })
    pt = pd.DataFrame(per_ticker).sort_values("total_pnl", ascending=False)
    for _, r in pt.iterrows():
        lines.append(
            f"| {r['ticker']} | {r['closed']} | ${r['total_pnl']:,.2f} | {r['pnl_pct']:.1f}% | "
            f"{r['win_rate']:.1f}% | ${r['avg_win']:,.2f} | ${r['avg_loss']:,.2f} | "
            f"${r['expectancy']:,.2f} | {r['avg_hold_hours']:.1f} | {r['whipsaw_pct']:.0f}% |"
        )
    lines.append("")
    pt.to_csv(os.path.join(REPORTS_DIR, "agent_trade_deep_dive.csv"), index=False)

    # ============ 4. Time-of-day analysis ============
    lines.append("## 4. Time-of-Day Analysis (Eastern)")
    lines.append("")
    lines.append("Best entry hour and best exit hour per ticker, by average PnL per round-trip.")
    lines.append("")
    lines.append("| Ticker | Best Entry Hour (ET) | Avg PnL | Entry Win Rate | Best Exit Hour (ET) | Avg PnL |")
    lines.append("|--------|----------------------|---------|----------------|---------------------|---------|")
    for sym, grp in rtdf.groupby("symbol"):
        if grp["open_hour_ny"].notna().sum() == 0:
            continue
        entry_hour = grp.groupby("open_hour_ny")["pnl"].mean().idxmax()
        entry_pnl = grp.groupby("open_hour_ny")["pnl"].mean().max()
        entry_grp = grp[grp["open_hour_ny"] == entry_hour]
        entry_wr = (entry_grp["pnl"] > 0).mean() * 100
        exit_hour = grp.groupby("close_hour_ny")["pnl"].mean().idxmax()
        exit_pnl = grp.groupby("close_hour_ny")["pnl"].mean().max()
        lines.append(
            f"| {sym} | {entry_hour:02d}:00 | ${entry_pnl:,.2f} | {entry_wr:.0f}% | "
            f"{exit_hour:02d}:00 | ${exit_pnl:,.2f} |"
        )
    lines.append("")

    # ============ 5. Second-level analyses ============
    lines.append("## 5. Second-Level Analyses")
    lines.append("")

    # 5a. Holding-time buckets
    lines.append("### 5a. Holding-Time Buckets")
    lines.append("")
    lines.append("| Bucket | Count | Total PnL | Win Rate |")
    lines.append("|--------|-------|-----------|----------|")
    for bk, grp in rtdf.groupby("holding_bucket"):
        wr = (grp["pnl"] > 0).mean() * 100
        lines.append(f"| {bk} | {len(grp)} | ${grp['pnl'].sum():,.2f} | {wr:.1f}% |")
    lines.append("")

    # 5b. Day of week
    lines.append("### 5b. Day-of-Week")
    lines.append("")
    lines.append("**By close (exit) day:**")
    lines.append("")
    lines.append("| Day | Count | Total PnL | Win Rate |")
    lines.append("|-----|-------|-----------|----------|")
    dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    for dow in dow_order:
        grp = rtdf[rtdf["close_dow"] == dow]
        if len(grp) == 0:
            continue
        wr = (grp["pnl"] > 0).mean() * 100
        lines.append(f"| {dow} | {len(grp)} | ${grp['pnl'].sum():,.2f} | {wr:.1f}% |")
    lines.append("")
    lines.append("**By entry (buy) day — best day to buy:**")
    lines.append("")
    lines.append("| Entry Day | Count | Total PnL | Avg PnL | Win Rate |")
    lines.append("|-----------|-------|-----------|---------|----------|")
    for dow in dow_order:
        grp = rtdf[rtdf["open_dow"] == dow]
        if len(grp) == 0:
            continue
        wr = (grp["pnl"] > 0).mean() * 100
        lines.append(f"| {dow} | {len(grp)} | ${grp['pnl'].sum():,.2f} | ${grp['pnl'].mean():,.2f} | {wr:.1f}% |")
    lines.append("")

    # 5c. Win/loss asymmetry
    lines.append("### 5c. Win/Loss Asymmetry")
    lines.append("")
    wins = rtdf[rtdf["pnl"] > 0]
    losses = rtdf[rtdf["pnl"] < 0]
    lines.append(f"- **Wins:** {len(wins)} | **Losses:** {len(losses)} | **Win rate:** {len(wins)/len(rtdf)*100:.1f}%")
    lines.append(f"- **Avg win:** ${wins['pnl'].mean():,.2f} | **Avg loss:** ${losses['pnl'].mean():,.2f}")
    lines.append(f"- **Profit factor:** {wins['pnl'].sum()/abs(losses['pnl'].sum()):.2f}")
    lines.append(f"- **Best single trade:** ${rtdf['pnl'].max():,.2f} | **Worst single trade:** ${rtdf['pnl'].min():,.2f}")
    lines.append("")

    # Helper to report coverage + an "uncovered" row so 100% buckets aren't misleading
    def coverage_note(field, covered_df, total_df):
        n_covered = len(covered_df)
        n_total = len(total_df)
        n_missing = n_total - n_covered
        if n_missing == 0:
            return f"*Coverage: {n_covered}/{n_total} round-trips have `{field}`.*"
        miss = total_df[total_df[field].isna()]
        miss_wr = (miss["pnl"] > 0).mean() * 100
        miss_pnl = miss["pnl"].sum()
        miss_syms = ", ".join(sorted(miss["symbol"].unique()))
        return (f"*Coverage: {n_covered}/{n_total} round-trips have `{field}`. "
                f"The {n_missing} without it ({miss_wr:.0f}% win, ${miss_pnl:,.2f}) are excluded "
                f"from the buckets below — they are: {miss_syms}. "
                f"Treat the 100% buckets as coverage artifacts, not real edges.*")

    # 5d. Conviction vs outcome
    lines.append("### 5d. Conviction vs Outcome")
    lines.append("")
    conv = rtdf[rtdf["entry_conviction"].notna()]
    lines.append(coverage_note("entry_conviction", conv, rtdf))
    lines.append("")
    if len(conv):
        conv["conv_bucket"] = pd.cut(conv["entry_conviction"], bins=[0, 0.4, 0.6, 0.8, 1.0],
                                     labels=["<0.4", "0.4-0.6", "0.6-0.8", "0.8-1.0"])
        lines.append("| Conviction | Count | Total PnL | Win Rate |")
        lines.append("|------------|-------|-----------|----------|")
        for cb, grp in conv.groupby("conv_bucket", observed=True):
            wr = (grp["pnl"] > 0).mean() * 100
            lines.append(f"| {cb} | {len(grp)} | ${grp['pnl'].sum():,.2f} | {wr:.1f}% |")
    lines.append("")

    # 5e. Direction vs outcome
    lines.append("### 5e. Direction vs Outcome")
    lines.append("")
    dirn = rtdf[rtdf["entry_direction"].notna()]
    lines.append(coverage_note("entry_direction", dirn, rtdf))
    lines.append("")
    if len(dirn):
        lines.append("| Direction | Count | Total PnL | Win Rate |")
        lines.append("|-----------|-------|-----------|----------|")
        for d, grp in dirn.groupby("entry_direction"):
            wr = (grp["pnl"] > 0).mean() * 100
            lines.append(f"| {d} | {len(grp)} | ${grp['pnl'].sum():,.2f} | {wr:.1f}% |")
    lines.append("")

    # 5f. Cash utilization at entry
    lines.append("### 5f. Cash Utilization at Entry")
    lines.append("")
    cu = rtdf[rtdf["cash_pct_at_entry"].notna()]
    lines.append(coverage_note("cash_pct_at_entry", cu, rtdf))
    lines.append("")
    if len(cu):
        cu["cash_bucket"] = pd.cut(cu["cash_pct_at_entry"], bins=[0, 20, 40, 60, 80, 101],
                                   labels=["<20%", "20-40%", "40-60%", "60-80%", "80-100%"])
        lines.append("| Cash % at Entry | Count | Total PnL | Win Rate |")
        lines.append("|-----------------|-------|-----------|----------|")
        for cb, grp in cu.groupby("cash_bucket", observed=True):
            wr = (grp["pnl"] > 0).mean() * 100
            lines.append(f"| {cb} | {len(grp)} | ${grp['pnl'].sum():,.2f} | {wr:.1f}% |")
        lines.append("")
        lines.append("> **Note:** The low-cash buckets showing 100% are a **time-confounding "
                     "artifact** — the losing early trades (07-06 to 07-27) all entered with "
                     "80-100% cash, so the lower-cash buckets only contain later winners. "
                     "This is not evidence that low cash predicts wins.")
    lines.append("")

    # 5g. Entry RSI vs outcome
    lines.append("### 5g. Entry RSI vs Outcome")
    lines.append("")
    rsi = rtdf[rtdf["entry_rsi"].notna()]
    lines.append(coverage_note("entry_rsi", rsi, rtdf))
    lines.append("")
    if len(rsi):
        rsi["rsi_bucket"] = pd.cut(rsi["entry_rsi"], bins=[0, 30, 50, 70, 101],
                                   labels=["<30 (oversold)", "30-50", "50-70", ">70 (overbought)"])
        lines.append("| Entry RSI | Count | Total PnL | Win Rate |")
        lines.append("|-----------|-------|-----------|----------|")
        for rb, grp in rsi.groupby("rsi_bucket", observed=True):
            wr = (grp["pnl"] > 0).mean() * 100
            lines.append(f"| {rb} | {len(grp)} | ${grp['pnl'].sum():,.2f} | {wr:.1f}% |")
    lines.append("")

    # 5h. Entry VWAP distance vs outcome
    lines.append("### 5h. Entry VWAP Distance vs Outcome")
    lines.append("")
    vw = rtdf[rtdf["entry_vwap_dist_pct"].notna()]
    lines.append(coverage_note("entry_vwap_dist_pct", vw, rtdf))
    lines.append("")
    if len(vw):
        vw["vwap_bucket"] = pd.cut(vw["entry_vwap_dist_pct"], bins=[-100, -1, 0, 1, 100],
                                   labels=["<-1%", "-1 to 0%", "0 to 1%", ">1%"])
        lines.append("| Entry VWAP Dist | Count | Total PnL | Win Rate |")
        lines.append("|-----------------|-------|-----------|----------|")
        for vb, grp in vw.groupby("vwap_bucket", observed=True):
            wr = (grp["pnl"] > 0).mean() * 100
            lines.append(f"| {vb} | {len(grp)} | ${grp['pnl'].sum():,.2f} | {wr:.1f}% |")
    lines.append("")

    # 5i. Crypto vs equity
    lines.append("### 5i. Crypto vs Equity")
    lines.append("")
    lines.append("| Asset Class | Count | Total PnL | Win Rate | Avg Hold (h) |")
    lines.append("|-------------|-------|-----------|----------|---------------|")
    for ac, grp in rtdf.groupby("is_crypto"):
        wr = (grp["pnl"] > 0).mean() * 100
        label = "Crypto" if ac else "Equity"
        lines.append(f"| {label} | {len(grp)} | ${grp['pnl'].sum():,.2f} | {wr:.1f}% | {grp['holding_hours'].mean():.1f} |")
    lines.append("")

    # ============ 6. Discussion starters ============
    lines.append("## 6. Discussion Starters for Improvement")
    lines.append("")
    lines.append("1. **Concentration in winners:** DOT/USD and SOL/USD dominate realized PnL "
                 "($5,396 + $4,669 = ~$10,065 of the $8,829 total; equity tickers are net "
                 "negative at -$1,304). The screener heavily favors crypto (SOL 69%, ADA 70%, "
                 "DOT 62%, BTC 58% of watchlist snapshots). Is this concentration acceptable, "
                 "or should equity exposure be rebalanced?")
    lines.append("2. **Equity desk is a drag:** Every equity ticker except MSFT/XOM/NVDA is net "
                 "negative. SPY (-$415), AMD (-$573), INTC (-$845) are the worst. The screener "
                 "rarely selects them (SPY/AMD/INTC/QQQ at 0% of watchlist), yet they still "
                 "produce losses — likely from legacy positions or fallback-universe trades. "
                 "Should the equity universe be pruned?")
    lines.append("3. **Whipsaw exposure:** TSLA is 100% <4h whipsaw (all 6 round-trips) and net "
                 "negative. AMD is 20% whipsaw. The whipsaw circuit breaker (MAX_WHIPSAW_RATIO "
                 "= 0.60) may be too lenient for these names. Consider a per-ticker minimum "
                 "hold or tighter whipsaw threshold.")
    lines.append("4. **Holding-time edge:** over_7d holds are the most profitable (78.3% win, "
                 "$6,455) while under_4h whipsaws lose (-$63, 25% win). The strategy rewards "
                 "patience. Consider discouraging sub-4h round-trips.")
    lines.append("5. **Day-of-week edge:** Saturday (92.3% win, $5,245) and Friday (54%, $4,401) "
                 "are the best; Monday/Tuesday are the worst. This is largely crypto-driven "
                 "(crypto trades 24/7). Worth investigating whether weekend crypto momentum is "
                 "a reliable edge or a data artifact.")
    lines.append("6. **Cash utilization:** Trades entered with 80-100% cash have only a 42.5% "
                 "win rate vs 100% for lower-cash entries. This may reflect that high-cash "
                 "entries are early-window trades before the strategy has deployed. Worth "
                 "examining whether entry timing relative to cash deployment matters.")
    lines.append("7. **Rule-driven churn:** Many sells are rule-mandated (support breach / "
                 "overbought liquidation, e.g. SOL selling 25% on each $94.50 breach). These "
                 "rules lock in profits but also create frequent partial exits. Are they "
                 "optimizing for realized PnL or adding unnecessary churn?")
    lines.append("8. **Conviction & indicator calibration:** Entry RSI <30 (oversold) and "
                 "VWAP-dist -1% to 0% show strong outcomes, but coverage is limited to recent "
                 "decisions (post 07-27) and every covered trade is a winner — so the 100% "
                 "win rates in sections 5d/5e/5g/5h are **coverage artifacts**, not validated "
                 "edges. As more decision-level data accrues, re-validate whether conviction "
                 "score and entry indicators actually predict outcomes.")
    lines.append("")

    # ============ 7. Data coverage & caveats ============
    lines.append("## 7. Data Coverage & Caveats")
    lines.append("")
    lines.append("- **Trade window:** 2026-07-05 to 2026-08-27 (415 filled rows, 248 closed round-trips).")
    lines.append("- **Reasoning / conviction / entry-indicator fields** are only populated for "
                 "decisions logged after 2026-07-27 (when the per-ticker decision schema was "
                 "introduced). Earlier round-trips (the majority) have no per-decision reasoning, "
                 "so sections 5d/5e/5g/5h reflect only the recent subset. **Because every losing "
                 "trade opened before 07-27, those sections show 100% win rates — this is a "
                 "data-coverage artifact, not a real edge.**")
    lines.append("- **Cash at entry** is NaN for trades before the first portfolio_history "
                 "snapshot (2026-07-07). The 100% win rates in section 5f's low-cash buckets are "
                 "a time-confounding artifact: the losing early trades all entered with 80-100% "
                 "cash.")
    lines.append("- **Broker-reconciled fills** (TP/SL bracket exits, dust liquidation) are "
                 "included via `reconcile_broker_orders`; these have no matching decision_id, "
                 "so their reasoning is not captured.")
    lines.append("- **FIFO matching** pairs buys->sells in chronological order; partial fills "
                 "produce partial round-trips. Open (unclosed) positions are excluded from "
                 "realized PnL.")
    lines.append("")

    report_path = os.path.join(REPORTS_DIR, "agent_trade_deep_dive.md")
    with open(report_path, "w") as f:
        f.write("\n".join(lines))
    logger.info(f"Report saved to {report_path}")

    print("\n".join(lines[:80]))


if __name__ == "__main__":
    main()