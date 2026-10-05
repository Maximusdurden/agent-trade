#!/usr/bin/env python3
"""SPY/QQQ/IWM 0DTE strike selector + position sizer (Components 4 & 5).

Implements the "latch-on" strategy's contract-selection and sizing layers:

  COMPONENT 4 — STRIKE SELECTOR (§6.1, §6.2, §6.4)
    - Restrict to the FIRST OTM strike (or ATM if spread is identical).
    - Target delta 0.40-0.50 when greeks are available (live accounts).
    - Round-number rule: never buy into resistance; no entry within $0.50
      below a major psychological round number; a clean 1-min close above a
      round number = breakout trigger.
    - On paper accounts (no greeks), fall back to the first-OTM-strike rule,
      which naturally lands near delta 0.40-0.50 for 0DTE.

  COMPONENT 5 — POSITION SIZER (§6.6)
    - Fixed dollar allocation: Contracts = floor($500 / (ask * 100)).
    - A 20% stop on a fixed $500 position = exact max loss of -$100 (1R).

This module is importable (for the full strategy engine) and runnable
standalone for a smoke test.

Usage:
    python -m sideload.options_strike_sizer --date 2026-09-25 --symbol SPY --direction BULLISH
    python -m sideload.options_strike_sizer --date 2026-09-25 --symbol SPY --direction BULLISH --no-discord
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("OptionsStrikeSizer")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
ET = ZoneInfo("America/New_York")

# Strategy universe.
TICKERS = ["SPY", "QQQ", "IWM"]

# Position sizing (§6.6).
BASE_ALLOCATION = 500.0  # $500 per trade

# Delta target (§6.2).
DELTA_MIN = 0.40
DELTA_MAX = 0.50

# Round-number buffer (§6.4): no entry within $0.50 below a round number.
ROUND_NUMBER_BUFFER = 0.50
# Round-number step (major psychological levels).
ROUND_NUMBER_STEP = 10.0

# Front-week expiry resolution (Phase 2).
FRIDAY_CUTOFF_TIME = dtime(12, 0, 0)  # After 12:00 PM ET on Friday -> next Friday.
# Contract sizing elasticity (Phase 2): allow 1 contract up to this notional.
ELASTIC_MAX_NOTIONAL = 650.0


def resolve_front_week_expiry(now_et: datetime | None = None) -> str:
    """Resolve the front-week (nearest Friday) option expiry.

    Rules:
      - Monday-Friday: target the current week's Friday.
      - Friday after 12:00 PM ET, or a weekend: target next Friday.

    Returns:
        Expiry date as 'YYYY-MM-DD' (ET).
    """
    now = now_et or datetime.now(ET)
    weekday = now.weekday()  # Mon=0 ... Sun=6
    # Days until this week's Friday (weekday 4).
    days_until_friday = (4 - weekday) % 7
    target = now.date() + timedelta(days=days_until_friday)

    # If today is Friday and it's after the cutoff, roll to next Friday.
    if weekday == 4 and now.time() >= FRIDAY_CUTOFF_TIME:
        target = target + timedelta(days=7)
    # If today is Saturday/Sunday, the modulo already rolled to next Friday.

    return target.strftime("%Y-%m-%d")


def _to_et(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(ET)


def _parse_occ(symbol: str) -> dict | None:
    """Parse an OCC option symbol into root/expiry/type/strike."""
    clean = str(symbol or "").replace(" ", "").upper()
    match = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", clean)
    if not match:
        return None
    root, date_str, type_char, strike_str = match.groups()
    try:
        expiration_date = datetime.strptime(date_str, "%y%m%d").date()
    except ValueError:
        return None
    return {
        "root": root,
        "expiration_date": expiration_date,
        "type": "CALL" if type_char == "C" else "PUT",
        "strike_price": float(strike_str) / 1000.0,
        "symbol": clean,
    }


def _is_round_number(price: float, step: float = ROUND_NUMBER_STEP) -> bool:
    """Return True if ``price`` is within ROUND_NUMBER_BUFFER below a round number."""
    if price <= 0:
        return False
    # Round numbers are multiples of step (e.g. 500, 510, 520 for step=10).
    nearest = round(price / step) * step
    # "Within $0.50 below" means price is in [nearest - buffer, nearest).
    return (nearest - ROUND_NUMBER_BUFFER) <= price < nearest


def _get_greeks_delta(snapshot) -> float | None:
    """Extract delta from a snapshot's greeks if available."""
    greeks = getattr(snapshot, "greeks", None)
    if greeks is None:
        return None
    delta = getattr(greeks, "delta", None)
    if delta is None:
        return None
    return float(delta)


def _delta_target_fallback(candidates: list[dict], opt_type: str,
                           max_notional: float) -> dict | None:
    """Find a delta 0.40-0.50 strike that fits within max_notional.

    Used when the first-OTM strike's premium is too expensive (notional >
    ELASTIC_MAX_NOTIONAL). A delta 0.40-0.50 strike is further OTM and
    cheaper, so it often fits where the first-OTM does not.

    Prefers OTM strikes, then delta closest to 0.45, then lowest ask.
    Returns None when no candidate fits (greeks unavailable or all too
    expensive).
    """
    target = [
        c for c in candidates
        if c["delta"] is not None
        and 0.40 <= abs(c["delta"]) <= 0.50
        and c["ask"] * 100.0 <= max_notional
    ]
    if not target:
        return None
    # Prefer OTM (dist >= 0), then delta closest to 0.45, then lowest ask.
    target.sort(key=lambda c: (0 if c["dist"] >= 0 else 1,
                               abs(abs(c["delta"]) - 0.45),
                               c["ask"]))
    return target[0]


def select_strike(client: AlpacaClient, symbol: str, direction: str,
                  session_date: str, current_price: float | None = None) -> dict:
    """Select the target 0DTE strike for a symbol/direction.

    Args:
        client: AlpacaClient.
        symbol: Ticker (SPY/QQQ/IWM).
        direction: 'BULLISH' (call) or 'BEARISH' (put).
        session_date: YYYY-MM-DD (ET).
        current_price: Optional pre-fetched underlying price.

    Returns:
        Dict with the selected contract info, or {} if none found.
    """
    opt_type = "call" if direction.upper() == "BULLISH" else "put"

    # 1. Current underlying price.
    if not current_price:
        try:
            current_price = client.get_latest_price(symbol.upper())
        except Exception as e:
            logger.error(f"Failed to get latest price for {symbol}: {e}")
            return {}
        if not current_price:
            return {}

    # 2. Fetch the 0DTE chain (expiry = session date).
    chain = client.get_option_chain_snapshot(
        underlying_symbol=symbol.upper(),
        expiration_date_gte=session_date,
        expiration_date_lte=session_date,
        contract_type=opt_type,
    )
    if not chain:
        logger.warning(f"No 0DTE {opt_type} chain for {symbol} on {session_date}.")
        return {}

    # 3. Parse all contracts, compute strike distance and delta.
    candidates = []
    for occ, snapshot in chain.items():
        parsed = _parse_occ(occ)
        if not parsed:
            continue
        quote = getattr(snapshot, "latest_quote", None)
        if quote is None:
            continue
        ask = float(getattr(quote, "ask_price", 0) or 0)
        bid = float(getattr(quote, "bid_price", 0) or 0)
        if ask <= 0:
            continue
        strike = parsed["strike_price"]
        # Distance from spot (positive = OTM for the direction).
        if opt_type == "call":
            dist = strike - current_price
        else:
            dist = current_price - strike
        delta = _get_greeks_delta(snapshot)
        candidates.append({
            "occ": occ,
            "strike": strike,
            "bid": bid,
            "ask": ask,
            "dist": dist,
            "delta": delta,
        })

    if not candidates:
        logger.warning(f"No valid contracts in {symbol} {opt_type} chain.")
        return {}

    # 4. Apply round-number rule: skip strikes within $0.50 below a round number.
    filtered = [c for c in candidates if not _is_round_number(c["strike"])]
    if not filtered:
        logger.warning(f"All {symbol} {opt_type} strikes are within round-number buffer; no entry.")
        return {}

    # 5. Select the first OTM strike (smallest positive distance).
    #    Prefer strikes with delta in [0.40, 0.50] when greeks available.
    otm = [c for c in filtered if c["dist"] >= 0]
    if not otm:
        # All strikes are ITM; fall back to the closest to ATM.
        otm = sorted(filtered, key=lambda c: abs(c["dist"]))[:1]

    # Sort by distance ascending (first OTM first), then by delta proximity to 0.45.
    otm.sort(key=lambda c: (c["dist"], abs((c["delta"] or 0.45) - 0.45)))
    selected = otm[0]

    # 6. Compute position size (§6.6) with Phase 2 elasticity.
    contracts = int(math.floor(BASE_ALLOCATION / (selected["ask"] * 100.0)))
    reject_reason = None
    selection_note = None
    if contracts < 1:
        primary_notional = selected["ask"] * 100.0
        if primary_notional <= ELASTIC_MAX_NOTIONAL:
            # Prevent allocation starvation on TSLA options: allow 1 contract.
            contracts = 1
            logger.info(
                f"Contract ask ${selected['ask']:.2f} exceeds ${BASE_ALLOCATION} "
                f"allocation but <= ${ELASTIC_MAX_NOTIONAL:.0f}; allowing 1 contract."
            )
        else:
            # First-OTM strike too expensive. Fall back to a delta 0.40-0.50
            # strike (further OTM, cheaper premium) when one fits the cap.
            fallback = _delta_target_fallback(filtered, opt_type, ELASTIC_MAX_NOTIONAL)
            if fallback is not None:
                selection_note = (
                    f"First-OTM {selected['occ']} too expensive "
                    f"(notional ${primary_notional:.2f} > ${ELASTIC_MAX_NOTIONAL:.0f}); "
                    f"fell back to delta-target {fallback['occ']} "
                    f"ask=${fallback['ask']:.2f} delta={fallback['delta']:.3f}."
                )
                logger.info(selection_note)
                selected = fallback
                contracts = 1
            else:
                reject_reason = (
                    f"Contract ask ${selected['ask']:.2f} too expensive "
                    f"(notional ${primary_notional:.2f} > ${ELASTIC_MAX_NOTIONAL:.0f}); "
                    f"no delta 0.40-0.50 fallback within ${ELASTIC_MAX_NOTIONAL:.0f}."
                )
                logger.warning(reject_reason)
                contracts = 0

    return {
        "symbol": symbol,
        "date": session_date,
        "direction": direction.upper(),
        "current_price": round(current_price, 2),
        "selected_occ": selected["occ"],
        "strike": selected["strike"],
        "bid": selected["bid"],
        "ask": selected["ask"],
        "delta": selected["delta"],
        "contracts": contracts,
        "notional": round(selected["ask"] * 100.0 * contracts, 2),
        "max_loss_20pct": round(selected["ask"] * 100.0 * contracts * 0.20, 2),
        "round_number_checked": True,
        "reject_reason": reject_reason,
        "selection_note": selection_note,
    }


def run(symbol: str, direction: str, session_date: str,
        send_discord: bool = True) -> dict:
    """Run the strike selector + position sizer for a symbol/direction."""
    client = AlpacaClient()
    result = select_strike(client, symbol, direction, session_date)
    if not result:
        logger.warning(f"No tradeable contract for {symbol} {direction} on {session_date}.")
        return {"symbol": symbol, "date": session_date, "direction": direction,
                "selected_occ": None, "contracts": 0}

    logger.info(
        f"[{symbol} {direction}] strike={result['strike']} ask={result['ask']} "
        f"delta={result['delta']} contracts={result['contracts']} "
        f"notional=${result['notional']} max_loss=${result['max_loss_20pct']}"
    )

    # Persist JSON.
    out_path = os.path.join(OUT_DIR, f"options_strike_sizer_{symbol}_{session_date}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)
    logger.info(f"Wrote results to {out_path}")

    if send_discord:
        try:
            send_discord_message(
                f"**Options Strike/Sizer — {symbol} {direction} {session_date}**\n"
                f"Strike: {result['strike']} | Ask: ${result['ask']} | "
                f"Delta: {result['delta']} | Contracts: {result['contracts']} | "
                f"Notional: ${result['notional']} | Max loss (20%): ${result['max_loss_20pct']}"
            )
        except Exception as e:
            logger.warning(f"Discord notification failed (non-fatal): {e}")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="SPY/QQQ/IWM 0DTE strike + sizer")
    parser.add_argument("--date", default=datetime.now(ET).strftime("%Y-%m-%d"),
                        help="Session date YYYY-MM-DD (ET). Default: today.")
    parser.add_argument("--symbol", required=True, choices=TICKERS,
                        help="Underlying ticker.")
    parser.add_argument("--direction", required=True, choices=["BULLISH", "BEARISH"],
                        help="Signal direction.")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord notification.")
    args = parser.parse_args()

    setup_jira_logging(app_name="agent-trade-sideload")
    try:
        result = run(args.symbol, args.direction, args.date,
                     send_discord=not args.no_discord)
        print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        log_exception_to_jira(e, "options_strike_sizer",
                              {"symbol": args.symbol, "direction": args.direction,
                               "date": args.date})
        logger.exception("options_strike_sizer failed")
        raise


if __name__ == "__main__":
    main()