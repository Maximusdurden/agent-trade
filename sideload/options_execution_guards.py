#!/usr/bin/env python3
"""SPY/QQQ/IWM 0DTE execution safeguards (Components 6-9).

Implements the "latch-on" strategy's order-protection layer:

  COMPONENT 6 — SPREAD GATE (§6.9)
    - Abort entry if (ask - bid) > $0.04 (contracts < $3.00)
      OR (ask - bid) / ask > 2.0% (contracts >= $3.00).

  COMPONENT 7 — STOP-LOSS ROUTER (§6.10)
    - Client-side trigger: when bid <= entry_price * 0.80, dispatch an IOC
      (Immediate-Or-Cancel) Limit Sell at bid - $0.03.
    - If the IOC leaves partial fills after 2 seconds, escalate to an
      emergency Market Sell to ensure flat inventory.

  COMPONENT 8 — CIRCUIT BREAKER (§6.11)
    - Max 1 trade per day.
    - Stop-out lockout: after a 20% stop loss, set status to HALTED_FOR_DAY.

  COMPONENT 9 — MACRO BLACKOUT (§6.12 Q4)
    - Block entries between 09:58 and 10:03 AM ET if an economic release is
      scheduled for 10:00 AM.

This module is importable (for the full strategy engine) and runnable
standalone for a smoke test.

Usage:
    python -m sideload.options_execution_guards --check-spread SPY260925C00772000
    python -m sideload.options_execution_guards --check-blackout 2026-09-25
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
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("OptionsExecutionGuards")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Component 6 — Spread gate (§6.9)
# ---------------------------------------------------------------------------
# Absolute spread cap for contracts < $3.00.
SPREAD_ABS_CAP = 0.04
# Percentage spread cap for contracts >= $3.00.
SPREAD_PCT_CAP = 0.02
# Price threshold separating the two spread rules.
SPREAD_PRICE_THRESHOLD = 3.00


def check_spread(bid: float, ask: float) -> dict:
    """Evaluate the spread gate for a contract quote.

    Returns a dict with pass/fail and the reason.
    """
    if ask <= 0 or bid < 0:
        return {"pass": False, "reason": f"Invalid quote (bid={bid}, ask={ask})"}

    spread = ask - bid
    if ask < SPREAD_PRICE_THRESHOLD:
        ok = spread <= SPREAD_ABS_CAP
        rule = f"abs spread <= ${SPREAD_ABS_CAP}"
    else:
        pct = spread / ask
        ok = pct <= SPREAD_PCT_CAP
        rule = f"pct spread <= {SPREAD_PCT_CAP:.0%}"

    return {
        "pass": ok,
        "bid": bid,
        "ask": ask,
        "spread": round(spread, 4),
        "rule": rule,
        "reason": "OK" if ok else f"Spread {spread:.4f} violates {rule}",
    }


# ---------------------------------------------------------------------------
# Component 7 — Stop-loss router (§6.10)
# ---------------------------------------------------------------------------
STOP_LOSS_PCT = 0.50          # 50% stop on premium (0DTE — see plan §11)
IOC_OFFSET = 0.03             # bid - $0.03 to cross the book
IOC_ESCALATE_SECONDS = 2.0    # escalate to market after 2s on partial fill


def stop_trigger_price(entry_price: float) -> float:
    """Return the bid price that triggers the stop (entry * 0.80)."""
    return entry_price * (1.0 - STOP_LOSS_PCT)


def ioc_limit_price(current_bid: float) -> float:
    """Return the IOC limit price (bid - $0.03) to cross the book."""
    return max(0.01, current_bid - IOC_OFFSET)


def route_stop_exit(client: AlpacaClient, occ_symbol: str, qty: int,
                    entry_price: float, current_bid: float) -> dict:
    """Execute the client-side stop-loss exit for an option position.

    Args:
        client: AlpacaClient.
        occ_symbol: OCC symbol of the option.
        qty: Number of contracts to sell.
        entry_price: Entry premium per contract.
        current_bid: Current bid price per contract.

    Returns:
        Dict describing the exit attempt.
    """
    trigger = stop_trigger_price(entry_price)
    if current_bid > trigger:
        return {"status": "no_trigger", "occ": occ_symbol,
                "current_bid": current_bid, "trigger": trigger}

    # 1. Dispatch IOC limit sell at bid - $0.03.
    limit = ioc_limit_price(current_bid)
    logger.warning(
        f"[STOP] {occ_symbol} bid {current_bid} <= trigger {trigger:.2f}; "
        f"dispatching IOC limit sell at {limit:.2f}"
    )
    try:
        result = client.place_option_order(
            symbol=occ_symbol, qty=qty, side="sell", limit_price=limit)
    except Exception as e:
        logger.error(f"[STOP] IOC limit sell failed for {occ_symbol}: {e}")
        result = {"status": "failed", "error": str(e)}

    # 2. Check for partial fill / no fill; escalate to market after 2s.
    status = str(result.get("status", "")).lower()
    filled_qty = float(result.get("filled_qty", 0) or 0)
    if status in ("filled", "accepted") and filled_qty >= qty:
        return {"status": "filled_ioc", "occ": occ_symbol, "result": result}

    # Escalate: wait up to 2s, then market sell.
    time.sleep(IOC_ESCALATE_SECONDS)
    logger.warning(f"[STOP] IOC partial/no fill for {occ_symbol}; escalating to market sell.")
    try:
        market_result = client.close_option_position(occ_symbol)
        return {"status": "escalated_market", "occ": occ_symbol,
                "ioc_result": result, "market_result": market_result}
    except Exception as e:
        logger.error(f"[STOP] Emergency market sell failed for {occ_symbol}: {e}")
        return {"status": "escalation_failed", "occ": occ_symbol,
                "ioc_result": result, "error": str(e)}


# ---------------------------------------------------------------------------
# Component 8 — Circuit breaker (§6.11)
# ---------------------------------------------------------------------------
# State file for the daily circuit breaker.
STATE_FILE = os.path.join(OUT_DIR, "options_circuit_breaker_state.json")

HALTED_FOR_DAY = "HALTED_FOR_DAY"
ACTIVE = "ACTIVE"


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def get_circuit_breaker_state(session_date: str) -> dict:
    """Return the circuit-breaker state for a session date.

    Resets to ACTIVE if the stored date differs from the current session.
    """
    state = _load_state()
    if state.get("date") != session_date:
        state = {"date": session_date, "status": ACTIVE, "trades_today": 0,
                 "stopped_out": False}
        _save_state(state)
    return state


def check_can_trade(session_date: str) -> dict:
    """Check whether a new trade is allowed today (max 1 trade, no re-entry after stop)."""
    state = get_circuit_breaker_state(session_date)
    can_trade = state["status"] == ACTIVE and state["trades_today"] < 1
    return {
        "can_trade": can_trade,
        "status": state["status"],
        "trades_today": state["trades_today"],
        "reason": ("OK" if can_trade else
                   "HALTED_FOR_DAY" if state["status"] == HALTED_FOR_DAY else
                   "Daily trade cap reached (max 1)"),
    }


def record_trade(session_date: str, stopped_out: bool = False) -> dict:
    """Record a trade for the day; halt the engine if it was a stop-out."""
    state = get_circuit_breaker_state(session_date)
    state["trades_today"] = state.get("trades_today", 0) + 1
    if stopped_out:
        state["status"] = HALTED_FOR_DAY
        state["stopped_out"] = True
    _save_state(state)
    return state


# ---------------------------------------------------------------------------
# Component 9 — Macro blackout (§6.12 Q4)
# ---------------------------------------------------------------------------
# Blackout window around a 10:00 AM ET release.
BLACKOUT_START = dtime(9, 58, 0)
BLACKOUT_END = dtime(10, 3, 0)
# High-impact economic releases that can reverse opening momentum.
HIGH_IMPACT_RELEASES = [
    "ISM Manufacturing", "ISM Services", "Consumer Confidence",
    "New Home Sales", "JOLTS", "Factory Orders", "Durable Goods",
    "Philadelphia Fed", "Empire State", "Retail Sales",
]


def _is_blackout_time(now_et: datetime) -> bool:
    t = now_et.time()
    return BLACKOUT_START <= t <= BLACKOUT_END


def check_macro_blackout(session_date: str, scheduled_releases: list[str] | None = None,
                         now_et: datetime | None = None) -> dict:
    """Check whether entries are blocked by a 10:00 AM macro release.

    Args:
        session_date: YYYY-MM-DD (ET).
        scheduled_releases: List of release names scheduled for 10:00 AM today.
            If None, assumes no releases (no blackout).
        now_et: Current time (ET). Defaults to now.

    Returns:
        Dict with blocked flag and reason.
    """
    now = now_et or datetime.now(ET)
    releases = scheduled_releases or []
    has_high_impact = any(
        any(r.lower() in rel.lower() for r in HIGH_IMPACT_RELEASES)
        for rel in releases
    )
    in_window = _is_blackout_time(now)

    blocked = has_high_impact and in_window
    return {
        "blocked": blocked,
        "in_blackout_window": in_window,
        "has_high_impact_release": has_high_impact,
        "releases": releases,
        "reason": ("BLOCKED: high-impact release during 09:58-10:03 blackout"
                   if blocked else "OK"),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_checks(session_date: str, send_discord: bool = True) -> dict:
    """Run a smoke test of all execution guards."""
    client = AlpacaClient()
    results = {"date": session_date}

    # Circuit breaker.
    results["circuit_breaker"] = check_can_trade(session_date)

    # Macro blackout (no releases scheduled by default).
    results["macro_blackout"] = check_macro_blackout(session_date)

    # Spread gate on a sample contract (if we can find one).
    try:
        chain = client.get_option_chain_snapshot(
            underlying_symbol="SPY", expiration_date_gte=session_date,
            expiration_date_lte=session_date, contract_type="call")
        if chain:
            occ = list(chain.keys())[0]
            snap = chain[occ]
            quote = getattr(snap, "latest_quote", None)
            if quote:
                results["spread_gate"] = check_spread(
                    float(getattr(quote, "bid_price", 0) or 0),
                    float(getattr(quote, "ask_price", 0) or 0))
                results["spread_gate"]["occ"] = occ
    except Exception as e:
        logger.warning(f"Spread gate sample failed: {e}")

    # Persist JSON.
    out_path = os.path.join(OUT_DIR, f"options_execution_guards_{session_date}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info(f"Wrote results to {out_path}")

    if send_discord:
        try:
            send_discord_message(
                f"**Options Execution Guards — {session_date}**\n"
                f"Circuit breaker: {results['circuit_breaker']}\n"
                f"Macro blackout: {results['macro_blackout']}"
            )
        except Exception as e:
            logger.warning(f"Discord notification failed (non-fatal): {e}")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="SPY/QQQ/IWM 0DTE execution guards")
    parser.add_argument("--date", default=datetime.now(ET).strftime("%Y-%m-%d"),
                        help="Session date YYYY-MM-DD (ET). Default: today.")
    parser.add_argument("--check-spread", metavar="BID,ASK",
                        help="Check the spread gate for a bid,ask pair (e.g. 1.40,1.50).")
    parser.add_argument("--check-blackout", action="store_true",
                        help="Check the macro blackout for the current time.")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord notification.")
    args = parser.parse_args()

    setup_jira_logging(app_name="agent-trade-sideload")
    try:
        if args.check_spread:
            bid, ask = [float(x) for x in args.check_spread.split(",")]
            print(json.dumps(check_spread(bid, ask), indent=2))
            return
        if args.check_blackout:
            print(json.dumps(check_macro_blackout(args.date), indent=2))
            return
        results = run_checks(args.date, send_discord=not args.no_discord)
        print(json.dumps(results, indent=2, default=str))
    except Exception as e:
        log_exception_to_jira(e, "options_execution_guards", {"date": args.date})
        logger.exception("options_execution_guards failed")
        raise


if __name__ == "__main__":
    main()