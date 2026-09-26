#!/usr/bin/env python3
"""SPY/QQQ/IWM 0DTE latch-on strategy backtest (Component 11).

Simulates the full "latch-on" strategy on historical intraday data:

  Entry: 09:30-09:45 latch-on confirmation (break & hold above PMH for calls /
         below PML for puts, while above intraday VWAP).
  Exit:  20% hard stop on premium, trailing stop (5-min low for longs),
         momentum flip, or 11:30 hard time exit.

Because Alpaca paper has no OPRA option bars (quotes are current-only), we
backtest the UNDERLYING price move and model the option premium via a simple
delta proxy. This validates the directional edge and the exit rules.

Usage:
    python -m sideload.backtest_options_latch --symbol SPY --days 60
    python -m sideload.backtest_options_latch --symbol SPY --days 60 --no-discord
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("BacktestOptionsLatch")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
ET = ZoneInfo("America/New_York")

# Strategy universe.
TICKERS = ["SPY", "QQQ", "IWM"]

# Latch-on window.
LATCH_START = dtime(9, 30, 0)
LATCH_END = dtime(9, 45, 0)

# Exit rules.
STOP_LOSS_PCT = 0.50          # 50% hard stop on premium (0DTE — see plan §11)
HARD_EXIT_TIME = dtime(11, 30, 0)  # hard time exit
TRAIL_BARS = 5                # 5-min trailing stop

# Stop-loss sweep candidates (for --sweep-stop).
STOP_SWEEP = [0.20, 0.30, 0.40, 0.50, 0.60, 0.70]

# Option premium model: delta proxy for the first-OTM strike.
# For 0DTE, first-OTM delta is roughly 0.40-0.50. We model premium using the
# standard first-order Taylor expansion:
#   option_delta_gain = dollar_move * delta
#   decay_loss = (minutes_held / 15) * 0.015 * entry_premium
#   current_option_price = entry_premium + option_delta_gain - decay_loss
DELTA_PROXY = 0.45
# Realistic 0DTE morning base premium (per contract).
ENTRY_PREMIUM = 1.50          # ~$1.50 for SPY first-OTM 0DTE
# Theta decay: ~0.015 per 15 minutes of hold during 09:30-10:30.
THETA_DECAY_PER_15MIN = 0.015
TRAIL_GIVEBACK_PCT = 0.15     # trailing stop giveback from peak premium

INTRADAY_INTERVAL = "1min"


def _to_et(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(ET)


def _load_intraday(client: AlpacaClient, symbol: str, days_back: int) -> pd.DataFrame:
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
    return df


def _load_daily(client: AlpacaClient, symbol: str, limit: int) -> pd.DataFrame:
    df = client.get_historical_bars(symbol, limit=limit, timeframe_str="day")
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index(level=0, drop=True)
    df.index = pd.to_datetime(df.index)
    if df.index.tzinfo is not None:
        df.index = df.index.tz_convert(ET).tz_localize(None)
    df = df.sort_index()
    return df


def _compute_anchors(daily: pd.DataFrame, intraday: pd.DataFrame,
                     day: pd.Timestamp) -> dict:
    """Compute PDH/PDL/PDC, PMH/PML, and anchored VWAP for a day."""
    day_naive = day.tz_localize(None) if day.tzinfo is not None else day
    prior = daily[daily.index < day_naive]
    if prior.empty:
        return {}
    prior_row = prior.iloc[-1]
    pdh, pdl, pdc = float(prior_row["high"]), float(prior_row["low"]), float(prior_row["close"])

    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    day_bars = intraday[(intraday.index >= day_start) & (intraday.index < day_end)]
    if day_bars.empty:
        return {}

    pm_start = day_start.replace(hour=4, minute=0)
    pm_end = day_start.replace(hour=9, minute=29)
    pm_bars = day_bars[(day_bars.index >= pm_start) & (day_bars.index <= pm_end)]
    pmh = float(pm_bars["high"].max()) if not pm_bars.empty else None
    pml = float(pm_bars["low"].min()) if not pm_bars.empty else None

    vwap_start = day_start.replace(hour=9, minute=30)
    vwap_bars = day_bars[day_bars.index >= vwap_start]
    vwap = None
    if not vwap_bars.empty and {"close", "volume"}.issubset(vwap_bars.columns):
        tp = (vwap_bars["high"] + vwap_bars["low"] + vwap_bars["close"]) / 3.0
        vwap = float((tp * vwap_bars["volume"]).sum() / vwap_bars["volume"].sum())

    return {"pdh": pdh, "pdl": pdl, "pdc": pdc, "pmh": pmh, "pml": pml,
            "vwap": vwap}


def _latch_signal(day_bars: pd.DataFrame, day: pd.Timestamp, anchors: dict) -> str | None:
    """Return 'BULLISH' / 'BEARISH' / None based on latch-on confirmation.

    Improved spec (plan §12):
      - Bar Close Requirement: price must log a full 1-min candle close beyond
        PMH (calls) / PML (puts), not a wick or intra-minute touch.
      - Persistence Filter: price must hold beyond the level for at least 2
        consecutive 1-min bars (Close[t] > PMH AND Close[t-1] > PMH).
      - Dynamic VWAP Slope Gate: dVWAP_3m = VWAP[t] - VWAP[t-3].
          Bullish: dVWAP_3m > 0.  Bearish: dVWAP_3m < 0.
      - Minimum Breakout Clearance: Price >= PMH * 1.001 (+0.10%).
    """
    latch_start = day.replace(hour=LATCH_START.hour, minute=LATCH_START.minute)
    latch_end = day.replace(hour=LATCH_END.hour, minute=LATCH_END.minute)
    window = day_bars[(day_bars.index >= latch_start) & (day_bars.index <= latch_end)]
    if len(window) < 4:
        return None
    pmh, pml, vwap = anchors.get("pmh"), anchors.get("pml"), anchors.get("vwap")
    if pmh is None or pml is None or vwap is None:
        return None

    # Compute anchored VWAP series from the 09:30 open bar (cumulative).
    vwap_start = day.replace(hour=9, minute=30)
    vwap_bars = day_bars[day_bars.index >= vwap_start]
    vwap_series = None
    if not vwap_bars.empty and {"close", "volume"}.issubset(vwap_bars.columns):
        tp = (vwap_bars["high"] + vwap_bars["low"] + vwap_bars["close"]) / 3.0
        cum_pv = (tp * vwap_bars["volume"]).cumsum()
        cum_v = vwap_bars["volume"].cumsum()
        vwap_series = cum_pv / cum_v.replace(0, pd.NA)

    closes = window["close"].to_numpy()
    idx = window.index

    # Check each bar t (from index 1 onward) for the 2-bar hold.
    for i in range(1, len(window)):
        close_t = float(closes[i])
        close_prev = float(closes[i - 1])
        ts_t = idx[i]

        # VWAP slope: VWAP[t] - VWAP[t-3].
        if vwap_series is not None:
            # Find VWAP at t and t-3 (3 bars back in the full day series).
            vwap_t = _vwap_at(vwap_series, vwap_bars.index, ts_t)
            ts_3 = ts_t - timedelta(minutes=3)
            vwap_3 = _vwap_at(vwap_series, vwap_bars.index, ts_3)
            d_vwap = (vwap_t - vwap_3) if (vwap_t is not None and vwap_3 is not None) else None
        else:
            d_vwap = None

        # BULLISH: 2-bar hold above PMH with clearance + rising VWAP.
        if close_t >= pmh * 1.001 and close_prev >= pmh * 1.001:
            if d_vwap is not None and d_vwap > 0:
                return "BULLISH"

        # BEARISH: 2-bar hold below PML with clearance + falling VWAP.
        if close_t <= pml * 0.999 and close_prev <= pml * 0.999:
            if d_vwap is not None and d_vwap < 0:
                return "BEARISH"

    return None


def _vwap_at(vwap_series: pd.Series, vwap_index, ts) -> float | None:
    """Return the VWAP value at or before timestamp ``ts`` (no lookahead)."""
    # Only use bars up to and including ts (no future data).
    mask = vwap_index <= ts
    if not mask.any():
        return None
    vals = vwap_series[mask]
    if vals.empty:
        return None
    return float(vals.iloc[-1])


def _atr14(daily: pd.DataFrame) -> float:
    """Compute the 14-period ATR from daily bars (using the last 14 rows)."""
    if len(daily) < 15:
        return 0.0
    df = daily.tail(15)
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.tail(14).mean())


def _momentum_confirmation(day_bars: pd.DataFrame, day: pd.Timestamp,
                           direction: str) -> bool:
    """Momentum confirmation filter.

    At least 2 of the first 3 five-minute bars must close in the signal
    direction (up for BULLISH, down for BEARISH), AND the latest bar must be
    positive (in the signal direction).

    Uses only completed 5-min bars within the 09:30-09:45 latch window.
    """
    latch_start = day.replace(hour=LATCH_START.hour, minute=LATCH_START.minute)
    latch_end = day.replace(hour=LATCH_END.hour, minute=LATCH_END.minute)
    window = day_bars[(day_bars.index >= latch_start) & (day_bars.index <= latch_end)]
    if len(window) < 3:
        return False

    # Resample 1-min bars into 5-min bars (completed only).
    five_min = window["close"].resample("5min").last().dropna()
    if len(five_min) < 3:
        return False

    # Direction of each 5-min bar (close vs previous close).
    closes = five_min.to_numpy()
    moves = []
    for i in range(1, len(closes)):
        moves.append(closes[i] - closes[i - 1])

    if len(moves) < 3:
        return False

    # First 3 moves.
    first3 = moves[:3]
    if direction == "BULLISH":
        up_count = sum(1 for m in first3 if m > 0)
        latest_positive = moves[-1] > 0
    else:  # BEARISH
        up_count = sum(1 for m in first3 if m < 0)
        latest_positive = moves[-1] < 0

    return up_count >= 2 and latest_positive


def _sentiment_confluence(symbol: str, direction: str, session_date: str,
                          client: AlpacaClient) -> bool:
    """Sentiment confluence gate: the selected ticker must independently clear
    the threshold in the signal direction.

    Bullish: Score[Selected_Ticker] >= +0.40
    Bearish: Score[Selected_Ticker] <= -0.40
    """
    try:
        from sideload.options_sentiment_sr import score_sentiment
        s = score_sentiment(client, symbol, session_date)
        score = s["score"]
        if direction == "BULLISH":
            return score >= 0.40
        else:  # BEARISH
            return score <= -0.40
    except Exception as e:
        logger.warning(f"Sentiment confluence check failed for {symbol}: {e}")
        return False


def _sr_headroom(direction: str, current_price: float, anchors: dict,
                 atr: float, day_bars: pd.DataFrame, day: pd.Timestamp) -> bool:
    """S/R headroom & resistance-overhead filter.

    Call: assert (PDH - spot) >= 1.5 * ATR(14), OR a clean 1-min close above PDH.
    Put:  assert (spot - PDL) >= 1.5 * ATR(14), OR a clean 1-min close below PDL.
    """
    pdh, pdl = anchors.get("pdh"), anchors.get("pdl")
    if pdh is None or pdl is None:
        return True  # no anchor -> don't block
    min_headroom = 1.5 * atr

    if direction == "BULLISH":
        if (pdh - current_price) >= min_headroom:
            return True
        # Else require a clean 1-min close above PDH.
        return bool((day_bars["close"] > pdh).any())
    else:  # BEARISH
        if (current_price - pdl) >= min_headroom:
            return True
        # Else require a clean 1-min close below PDL.
        return bool((day_bars["close"] < pdl).any())


def _simulate_exit(day_bars: pd.DataFrame, day: pd.Timestamp, direction: str,
                   entry_price: float, stop_loss_pct: float = STOP_LOSS_PCT) -> dict:
    """Simulate the exit from entry to the 11:30 hard exit.

    Models option premium as: premium = entry_premium * (1 + delta*move - theta).
    Applies the hard stop (stop_loss_pct) and a trailing stop.
    """
    entry_ts = day.replace(hour=LATCH_END.hour, minute=LATCH_END.minute)
    exit_ts = day.replace(hour=HARD_EXIT_TIME.hour, minute=HARD_EXIT_TIME.minute)
    window = day_bars[(day_bars.index >= entry_ts) & (day_bars.index <= exit_ts)]
    if window.empty:
        return {"traded": False, "reason": "no_data"}

    entry_premium = ENTRY_PREMIUM
    premium = entry_premium
    peak_premium = entry_premium
    exit_premium = None
    exit_reason = "time_exit"
    # Track the underlying move for diagnostics.
    entry_underlying = entry_price
    exit_underlying = entry_price

    for ts, bar in window.iterrows():
        px = float(bar["close"])
        # Dollar move (signed by direction).
        if direction == "BEARISH":
            dollar_move = entry_price - px
        else:
            dollar_move = px - entry_price
        minutes = (ts - entry_ts).total_seconds() / 60.0
        # Correct premium model: dollar_move * delta - theta decay.
        option_delta_gain = dollar_move * DELTA_PROXY
        decay_loss = (minutes / 15.0) * THETA_DECAY_PER_15MIN * entry_premium
        premium = max(0.01, entry_premium + option_delta_gain - decay_loss)
        exit_underlying = px

        # Hard stop.
        if premium <= entry_premium * (1.0 - stop_loss_pct):
            exit_premium, exit_reason = premium, "stop_20pct"
            break
        # Trailing stop from peak.
        peak_premium = max(peak_premium, premium)
        if peak_premium > entry_premium:
            if (peak_premium - premium) / peak_premium >= TRAIL_GIVEBACK_PCT:
                exit_premium, exit_reason = premium, "trailing_stop"
                break
        exit_premium = premium

    if exit_premium is None:
        return {"traded": False, "reason": "no_data"}

    pnl_pct = (exit_premium - entry_premium) / entry_premium
    # Underlying directional move (signed by direction).
    underlying_move = (exit_underlying - entry_underlying) / entry_underlying
    if direction == "BEARISH":
        underlying_move = -underlying_move
    return {
        "traded": True,
        "direction": direction,
        "entry_premium": entry_premium,
        "exit_premium": round(exit_premium, 4),
        "exit_reason": exit_reason,
        "pnl_pct": round(pnl_pct * 100.0, 2),
        "underlying_move_pct": round(underlying_move * 100.0, 2),
    }


def run_backtest(client: AlpacaClient, symbol: str, days_back: int,
                 stop_loss_pct: float = STOP_LOSS_PCT,
                 use_sentiment: bool = True,
                 use_momentum: bool = False) -> dict:
    """Run the latch-on backtest for a symbol over N days.

    Args:
        use_sentiment: If True, apply the sentiment confluence gate. Set False
            to validate the price-action filters independently (the Alpaca News
            API only has ~5 days of history, so sentiment can't be validated
            on long windows).
        use_momentum: If True, apply the momentum confirmation filter (>=2 of
            first 3 five-min bars in direction + latest bar positive).
    """
    daily = _load_daily(client, symbol, limit=days_back + 5)
    intraday = _load_intraday(client, symbol, days_back)
    if daily.empty or intraday.empty:
        logger.error(f"No data for {symbol}.")
        return {}

    # Unique trading days from intraday.
    days = sorted(intraday.index.normalize().unique())
    results = []
    for day in days:
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        day_bars = intraday[(intraday.index >= day_start) & (intraday.index < day_end)]
        if day_bars.empty:
            continue
        anchors = _compute_anchors(daily, intraday, day)
        if not anchors:
            continue
        direction = _latch_signal(day_bars, day, anchors)
        if direction is None:
            continue
        # Entry price = last close in latch window.
        latch_end = day.replace(hour=LATCH_END.hour, minute=LATCH_END.minute)
        entry_price = float(day_bars[day_bars.index <= latch_end]["close"].iloc[-1])

        # Sentiment confluence gate: selected ticker must clear threshold.
        if use_sentiment and not _sentiment_confluence(symbol, direction, str(day.date()), client):
            continue

        # Momentum confirmation: >=2 of first 3 five-min bars in direction +
        # latest bar positive.
        if use_momentum and not _momentum_confirmation(day_bars, day, direction):
            continue

        # S/R headroom & resistance-overhead filter.
        atr = _atr14(daily)
        if not _sr_headroom(direction, entry_price, anchors, atr, day_bars, day):
            continue

        sim = _simulate_exit(day_bars, day, direction, entry_price,
                             stop_loss_pct=stop_loss_pct)
        if sim["traded"]:
            sim["date"] = str(day.date())
            sim["symbol"] = symbol
            results.append(sim)

    if not results:
        return {"symbol": symbol, "days": days_back, "trades": 0, "summary": {}}

    df = pd.DataFrame(results)
    wins = df[df["pnl_pct"] > 0]
    losses = df[df["pnl_pct"] <= 0]
    # Underlying directional move stats (independent of the premium model).
    um = df["underlying_move_pct"] if "underlying_move_pct" in df.columns else pd.Series(dtype=float)
    summary = {
        "trades": len(df),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(df) * 100.0, 1) if len(df) else 0.0,
        "avg_pnl_pct": round(float(df["pnl_pct"].mean()), 2),
        "total_pnl_pct": round(float(df["pnl_pct"].sum()), 2),
        "avg_win_pct": round(float(wins["pnl_pct"].mean()), 2) if len(wins) else 0.0,
        "avg_loss_pct": round(float(losses["pnl_pct"].mean()), 2) if len(losses) else 0.0,
        "avg_underlying_move_pct": round(float(um.mean()), 3) if len(um) else 0.0,
        "underlying_win_rate": round(float((um > 0).mean() * 100.0), 1) if len(um) else 0.0,
        "exit_reasons": df["exit_reason"].value_counts().to_dict(),
    }
    return {"symbol": symbol, "days": days_back, "trades": len(df),
            "summary": summary, "trades_detail": results}


def run_all(client: AlpacaClient, days_back: int, use_sentiment: bool = True,
            use_momentum: bool = False) -> dict:
    """Run the backtest for all tickers."""
    all_results = {}
    for sym in TICKERS:
        logger.info(f"Backtesting {sym} over {days_back} days...")
        all_results[sym] = run_backtest(client, sym, days_back,
                                        use_sentiment=use_sentiment,
                                        use_momentum=use_momentum)
    return all_results


def sweep_stop_loss(client: AlpacaClient, days_back: int) -> dict:
    """Sweep stop-loss percentages across all tickers to find the realistic level.

    Returns {symbol: {stop_pct: summary}}.
    """
    sweep_results = {}
    for sym in TICKERS:
        logger.info(f"Sweeping stop-loss for {sym} over {days_back} days...")
        per_symbol = {}
        for sl in STOP_SWEEP:
            res = run_backtest(client, sym, days_back, stop_loss_pct=sl)
            per_symbol[str(sl)] = res.get("summary", {})
        sweep_results[sym] = per_symbol
    return sweep_results


def main() -> None:
    parser = argparse.ArgumentParser(description="SPY/QQQ/IWM 0DTE latch-on backtest")
    parser.add_argument("--symbol", choices=TICKERS, default=None,
                        help="Ticker to backtest (default: all).")
    parser.add_argument("--days", type=int, default=60,
                        help="Days of history to backtest.")
    parser.add_argument("--sweep-stop", action="store_true",
                        help="Sweep stop-loss percentages to find the realistic level.")
    parser.add_argument("--no-sentiment", action="store_true",
                        help="Skip the sentiment confluence gate (validate price-action filters only).")
    parser.add_argument("--momentum", action="store_true",
                        help="Apply the momentum confirmation filter (>=2 of first 3 five-min bars in direction + latest bar positive).")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord notification.")
    args = parser.parse_args()

    setup_jira_logging(app_name="agent-trade-sideload")
    try:
        client = AlpacaClient()
        use_sentiment = not args.no_sentiment
        use_momentum = args.momentum
        if args.sweep_stop:
            results = sweep_stop_loss(client, args.days)
            out_path = os.path.join(OUT_DIR, "backtest_options_latch_stop_sweep.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, default=str)
            logger.info(f"Wrote stop sweep to {out_path}")

            # Print a compact table.
            print(f"{'Ticker':<8} {'Stop%':<8} {'Trades':<8} {'Win%':<8} {'AvgPnL%':<10} {'TotalPnL%':<10} {'UMWin%':<8}")
            for sym, per_sl in results.items():
                for sl, s in per_sl.items():
                    print(f"{sym:<8} {sl:<8} {s.get('trades',0):<8} "
                          f"{s.get('win_rate',0):<8} {s.get('avg_pnl_pct',0):<10} "
                          f"{s.get('total_pnl_pct',0):<10} {s.get('underlying_win_rate',0):<8}")
            return

        if args.symbol:
            results = {args.symbol: run_backtest(client, args.symbol, args.days,
                                                 use_sentiment=use_sentiment,
                                                 use_momentum=use_momentum)}
        else:
            results = run_all(client, args.days, use_sentiment=use_sentiment,
                              use_momentum=use_momentum)

        out_path = os.path.join(OUT_DIR, "backtest_options_latch.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str)
        logger.info(f"Wrote backtest to {out_path}")

        # Print summary.
        for sym, res in results.items():
            s = res.get("summary", {})
            print(f"{sym}: trades={s.get('trades',0)} win_rate={s.get('win_rate',0)}% "
                  f"avg_pnl={s.get('avg_pnl_pct',0)}% total={s.get('total_pnl_pct',0)}%")

        if not args.no_discord:
            try:
                lines = [f"**Options Latch Backtest ({args.days}d)**"]
                for sym, res in results.items():
                    s = res.get("summary", {})
                    lines.append(
                        f"`{sym}` trades={s.get('trades',0)} win={s.get('win_rate',0)}% "
                        f"avg={s.get('avg_pnl_pct',0)}% total={s.get('total_pnl_pct',0)}%"
                    )
                send_discord_message("\n".join(lines))
            except Exception as e:
                logger.warning(f"Discord notification failed (non-fatal): {e}")
    except Exception as e:
        log_exception_to_jira(e, "backtest_options_latch", {"days": args.days})
        logger.exception("backtest_options_latch failed")
        raise


if __name__ == "__main__":
    main()