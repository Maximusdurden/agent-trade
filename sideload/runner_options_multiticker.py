#!/usr/bin/env python3
"""Multi-ticker Options Model A production runner (Phase 3, Part B).

Refactor of runner_options_tsla.py into a multi-ticker priority engine.

Universe config: retains TSLA + any candidate tickers that achieve PF >= 1.50
in the Phase 3 screen. (Empirical screen result: only TSLA passed.)

Flow:
  09:29:50  Pre-market volatility gate for EACH ticker in the active universe.
            Compute (PMH - PML) / PML; disarm any ticker failing >= 0.35%.
  09:30-10:15  Intraday evaluation loop across all armed tickers concurrently.
            First-to-Fire Rule: first ticker to confirm Model A executes entry.
            Simultaneous Tie-Breaker: higher (PMH - PML) / PML ratio wins.
            Once an order dispatches, cancel all pending watches and transition
            to the 2-second client-side monitor loop for the active position.
            Hard account cap: exactly 1 trade per session.

Usage:
    python -m sideload.runner_options_multiticker --dry-run
    python -m sideload.runner_options_multiticker --live
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from sideload.options_strike_sizer import select_strike, resolve_front_week_expiry
from sideload.options_execution_guards import (
    check_spread, check_can_trade, record_trade, check_macro_blackout,
)
from sideload.runner_options_tsla import (
    _load_intraday,
    _load_daily,
    _vwap_series,
    _vwap_at,
    _pm_volatility_ok,
    _get_quote,
    _dispatch_exit,
    _monitor_position,
    _finalize_exit,
    _append_fill,
    load_state,
    save_state,
    sync_down_from_gcs,
    sync_up_to_gcs,
    STATE_PATH,
    TP_PCT,
    STOP_PCT,
    MAX_HOLD_MINUTES,
    PM_VOL_MIN,
    MODEL_A_START,
    MODEL_A_END,
    POLL_INTERVAL_SECONDS,
)
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("RunnerOptionsMultiTicker")

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Universe config (TSLA + approved candidates from Phase 3 screen).
# ---------------------------------------------------------------------------
# entry_premium / delta per ticker. Only tickers with PF >= 1.50 are retained.
TICKER_CONFIG = {
    "TSLA": {"entry_premium": 3.50, "delta": 0.45},
    # AMD/COIN/AMZN failed the PF >= 1.50 gate in the Phase 3 screen and are
    # excluded from the active universe. Add them back here only if they pass.
}
# Active universe = tickers retained above.
ACTIVE_UNIVERSE = list(TICKER_CONFIG.keys())

# Intraday evaluation loop cadence.
EVAL_INTERVAL_SECONDS = 2


def _pm_volatility_for(client: AlpacaClient, symbol: str, session_date: str) -> dict:
    """Compute the PM volatility gate for a specific ticker."""
    intraday = _load_intraday(client, symbol, days_back=5)
    if intraday.empty:
        return {"pass": False, "reason": "No intraday data", "pmh": None, "pml": None}

    day = pd.Timestamp(session_date, tz=ET)
    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    day_bars = intraday[(intraday.index >= day_start) & (intraday.index < day_end)]
    if day_bars.empty:
        return {"pass": False, "reason": "No bars on session date", "pmh": None, "pml": None}

    pm_start = day_start.replace(hour=4, minute=0)
    pm_end = day_start.replace(hour=9, minute=29)
    pm_bars = day_bars[(day_bars.index >= pm_start) & (day_bars.index <= pm_end)]
    if pm_bars.empty:
        return {"pass": False, "reason": "No pre-market bars", "pmh": None, "pml": None}

    pmh = float(pm_bars["high"].max())
    pml = float(pm_bars["low"].min())
    if pml <= 0:
        return {"pass": False, "reason": "Invalid PML", "pmh": pmh, "pml": pml}

    range_pct = (pmh - pml) / pml
    ok = range_pct >= PM_VOL_MIN
    return {
        "pass": ok,
        "reason": "OK" if ok else f"PM range {range_pct:.4%} < {PM_VOL_MIN:.4%}",
        "pmh": pmh,
        "pml": pml,
        "range_pct": round(range_pct * 100.0, 3),
    }


def _model_a_setup_for(client: AlpacaClient, symbol: str, session_date: str,
                       pmh: float, pml: float) -> dict | None:
    """Detect a Model A setup for a specific ticker on 1-min bar close."""
    intraday = _load_intraday(client, symbol, days_back=5)
    if intraday.empty:
        return None

    day = pd.Timestamp(session_date, tz=ET)
    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    day_bars = intraday[(intraday.index >= day_start) & (intraday.index < day_end)]
    if day_bars.empty:
        return None

    vwap_series = _vwap_series(day_bars, day)
    a_start = day.replace(hour=MODEL_A_START.hour, minute=MODEL_A_START.minute)
    a_end = day.replace(hour=MODEL_A_END.hour, minute=MODEL_A_END.minute)
    window = day_bars[(day_bars.index >= a_start) & (day_bars.index <= a_end)]
    if window.empty:
        return None

    for ts, bar in window.iterrows():
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
        vwap = _vwap_at(vwap_series, ts)

        # Bearish sweep: High > PMH, Close < PMH, Close < VWAP.
        if high > pmh and close < pmh and vwap is not None and close < vwap:
            return {"direction": "BEARISH", "entry_ts": ts, "entry_price": close}

        # Bullish sweep: Low < PML, Close > PML, Close > VWAP.
        if low < pml and close > pml and vwap is not None and close > vwap:
            return {"direction": "BULLISH", "entry_ts": ts, "entry_price": close}

    return None


def _enter_position(client: AlpacaClient, symbol: str, session_date: str,
                    setup: dict, dry_run: bool = False) -> dict | None:
    """Resolve expiry, select strike, size, and BUY the option for a ticker."""
    direction = setup["direction"]
    opt_type = "call" if direction == "BULLISH" else "put"
    cfg = TICKER_CONFIG[symbol]

    # 1. Resolve front-week expiry.
    expiry = resolve_front_week_expiry()
    logger.info(f"[{symbol}] Resolved front-week expiry: {expiry}")

    # 2. Select strike + size.
    strike_info = select_strike(client, symbol, direction, expiry,
                                current_price=setup["entry_price"])
    if not strike_info or not strike_info.get("selected_occ"):
        logger.warning(f"[{symbol}] No tradeable {opt_type} contract on {expiry}.")
        return None

    occ = strike_info["selected_occ"]
    contracts = int(strike_info.get("contracts", 0))
    if contracts < 1:
        logger.warning(f"[{symbol}] Contract sizing rejected {occ} (contracts=0).")
        return None

    ask = float(strike_info["ask"])
    # Spread gate.
    spread = check_spread(float(strike_info["bid"]), ask)
    if not spread["pass"]:
        logger.warning(f"[{symbol}] Spread gate failed for {occ}: {spread['reason']}")
        return None

    entry_premium = ask
    target_premium = round(entry_premium * (1.0 + TP_PCT), 2)
    stop_premium = round(entry_premium * (1.0 - STOP_PCT), 2)

    logger.info(f"[{symbol}] [ENTRY] {direction} {occ} {contracts}x ask={ask:.2f} "
                f"target={target_premium:.2f} stop={stop_premium:.2f}")

    if dry_run:
        fill_price = ask
        logger.info(f"[{symbol}] [DRY-RUN] Would BUY {contracts}x {occ} at {fill_price:.2f}")
        result = {"status": "dry_run", "filled_avg_price": fill_price}
    else:
        result = client.place_option_order(
            symbol=occ, qty=contracts, side="buy", limit_price=ask)
        fill_price = result.get("filled_avg_price")

    pos = {
        "contract_symbol": occ,
        "entry_time": datetime.now(ET).isoformat(),
        "entry_premium": entry_premium,
        "contracts": contracts,
        "target_premium": target_premium,
        "stop_premium": stop_premium,
        "direction": direction,
        "session_date": session_date,
        "entry_price": setup["entry_price"],
        "symbol": symbol,
    }
    return pos


def _evaluate_armed_tickers(client: AlpacaClient, session_date: str,
                            armed: dict) -> dict | None:
    """Poll all armed tickers and return the first Model A setup to fire.

    First-to-Fire Rule: the first ticker to confirm Model A executes.
    Simultaneous Tie-Breaker: if two tickers trigger on the same bar, the one
    with the higher (PMH - PML) / PML ratio wins.

    Returns a dict with 'symbol', 'setup', 'pm' for the winning ticker, or None.
    """
    fired = []
    for symbol, pm in armed.items():
        setup = _model_a_setup_for(client, symbol, session_date,
                                   pm["pmh"], pm["pml"])
        if setup is not None:
            fired.append({"symbol": symbol, "setup": setup, "pm": pm})

    if not fired:
        return None

    # Tie-breaker: highest PM range ratio wins.
    fired.sort(key=lambda f: f["pm"].get("range_pct", 0.0), reverse=True)
    return fired[0]


def run_session(session_date: str, dry_run: bool = True,
                universe: list[str] | None = None) -> dict:
    """Run one multi-ticker Model A session."""
    active = universe or ACTIVE_UNIVERSE
    client = AlpacaClient()
    sync_down_from_gcs()
    state = load_state()

    # Circuit breaker: max 1 trade/day.
    can_trade = check_can_trade(session_date)
    if not can_trade["can_trade"]:
        logger.info(f"Circuit breaker: {can_trade['reason']}")
        return {"status": "skipped", "reason": can_trade["reason"]}

    # Macro blackout (no scheduled releases by default).
    blackout = check_macro_blackout(session_date)
    if blackout["blocked"]:
        logger.info(f"Macro blackout: {blackout['reason']}")
        return {"status": "skipped", "reason": blackout["reason"]}

    # 1. Pre-market volatility gate for each ticker; disarm failures.
    armed = {}
    pm_results = {}
    for symbol in active:
        pm = _pm_volatility_for(client, symbol, session_date)
        pm_results[symbol] = pm
        if pm["pass"]:
            armed[symbol] = pm
            logger.info(f"[{symbol}] PM gate PASS (range {pm['range_pct']}%).")
        else:
            logger.info(f"[{symbol}] PM gate DISARMED: {pm['reason']}")

    if not armed:
        return {"status": "all_disarmed", "pm_results": pm_results}

    # 2. Intraday evaluation loop (09:30-10:15 ET).
    winner = None
    while True:
        now = datetime.now(ET)
        if now.time() > MODEL_A_END:
            logger.info("Model A window closed; no setup fired.")
            break

        winner = _evaluate_armed_tickers(client, session_date, armed)
        if winner is not None:
            logger.info(f"[{winner['symbol']}] Model A setup fired first.")
            break

        time.sleep(EVAL_INTERVAL_SECONDS)

    if winner is None:
        return {"status": "no_setup", "pm_results": pm_results, "armed": list(armed)}

    # 3. Enter position for the winning ticker.
    pos = _enter_position(client, winner["symbol"], session_date,
                          winner["setup"], dry_run=dry_run)
    if pos is None:
        return {"status": "no_entry", "pm_results": pm_results,
                "winner": winner["symbol"], "setup": winner["setup"]}

    # 4. Persist state + record trade. Cancel pending watches (single position).
    state["active_position"] = pos
    state["last_session"] = session_date
    save_state(state)
    sync_up_to_gcs()
    record_trade(session_date, stopped_out=False)

    # 5. Monitor position until exit.
    exit_info = _monitor_position(client, pos, dry_run=dry_run)

    # 6. Clear state.
    state["active_position"] = None
    save_state(state)
    sync_up_to_gcs()

    return {"status": "completed", "pm_results": pm_results,
            "winner": winner["symbol"], "setup": winner["setup"],
            "position": pos, "exit": exit_info}


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-ticker Options Model A runner (Phase 3)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate entry/exit without placing real orders.")
    parser.add_argument("--live", action="store_true",
                        help="Place real orders (default is dry-run).")
    parser.add_argument("--date", default=datetime.now(ET).strftime("%Y-%m-%d"),
                        help="Session date YYYY-MM-DD (ET). Default: today.")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord notification.")
    args = parser.parse_args()

    dry_run = not args.live
    setup_jira_logging(app_name="agent-trade-sideload")
    try:
        result = run_session(args.date, dry_run=dry_run)
        print(json.dumps(result, indent=2, default=str))

        if not args.no_discord:
            try:
                status = result.get("status", "unknown")
                lines = [f"**Multi-Ticker Options Model A — {args.date}**",
                         f"Mode: {'DRY-RUN' if dry_run else 'LIVE'} | Status: {status}"]
                if result.get("winner"):
                    lines.append(f"Winner: {result['winner']}")
                if result.get("exit"):
                    ex = result["exit"]
                    lines.append(f"Exit: {ex.get('reason')} | PnL: {ex.get('pnl_pct')}%")
                send_discord_message("\n".join(lines))
            except Exception as e:
                logger.warning(f"Discord notification failed (non-fatal): {e}")
    except Exception as e:
        log_exception_to_jira(e, "runner_options_multiticker",
                              {"date": args.date, "dry_run": dry_run})
        logger.exception("runner_options_multiticker failed")
        raise


if __name__ == "__main__":
    main()
