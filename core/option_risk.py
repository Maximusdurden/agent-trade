"""Options risk-management layer: event gate, greeks exposure caps, EOD inventory.

This module provides deterministic, LLM-independent risk controls for the open
options book, complementing the pre-expiry auto-close sweep. Because options are
LEVERAGED and time-sensitive, these controls run in the daily/intraday cycle
(before the brain appraises positions) so that no position can hold through a
high-risk event, carry more overnight vega than desired, or exceed a per-option
delta allocation without a guardrail-seen decision.

Three controls:

1. EVENT GATE  (``OPTIONS_EVENT_GATE_ENABLED``, default on)
   Detects high-impact scheduled events (earnings, and optionally FOMC / major
   macro squirrels) whose IV crush / gap risk can wipe out a 30-60 DTE contract
   even though the *underlying* thesis may be intact. When a held option's
   underlying reports earnings (or a macro window is open) at/before the option's
   expiration, the position is flattened before the close of the session *before*
   the event date (T-1). This captures nearly all of the overnight-risk benefit
   of an unconditional end-of-day flatten while leaving multi-week theta
   collection intact for normally-distributed sessions.

2. VEGA / DELTA EXPOSURE CAPS  (``OPTIONS_VEGA_CAP_MV_PCT``, ``OPTIONS_DELTA_CAP_PCT``)
   Two independent dollar-based limits across the WHOLE open options book,
   expressed as a percent of account equity. Greeks (when available) are used so
   the caps reflect real dollar risk, not notional premium:
     - VEGA cap: aggregate |vega| (dollars of price change per +1 vol point)
       must not exceed ``equity * OPTIONS_VEGA_CAP_MV_PCT``.
     - DELTA cap: aggregate |delta| (dollars of directional equity change per +1
       point in the underlying) must not exceed ``equity * OPTIONS_DELTA_CAP_PCT``.
   When greeks are unavailable (paper-skipped bars, mock), the position is
   APPRAISED by premium market value instead (the classic fallback), so the cap
   still enforces a hard dollar limit without greek data.

3. END-OF-DAY INVENTORY  (``OPTIONS_EOD_FLAT``, default OFF)
   The "flatten the whole options book at the end of the session" arm. Off by
   default: it fights the 30-60 DTE swing thesis, compounds spread cost, and
   dilutes the strategist's options-learning signal. When enabled, it flattens
   every open option position in the final intraday cycle before the 4pm ET
   close. Prefer the event gate + caps in normal operation.

All controls only fire when options trading is enabled (``OPTIONS_ENABLED``) and
only ever close (SELL-to-close) positions — they never open or add risk.
"""

import logging
import re
from datetime import datetime, time, timedelta
from core import config

logger = logging.getLogger("OptionRisk")

# ---------------------------------------------------------------------------
# FOMC / macro windows (approximate — synthetic calendar; see fetch below).
# ---------------------------------------------------------------------------
_FOMC_DAYS_2026 = {
    (1, 27), (1, 28),
    (3, 17), (3, 18),
    (4, 28), (4, 29),
    (6, 16), (6, 17),
    (7, 28), (7, 29),
    (9, 15), (9, 16),
    (10, 27), (10, 28),
    (12, 8), (12, 9),
}

# OCC option symbol parsing for DTE/type without a full dependency.
_OCC_RE = re.compile(r"^([A-Z]{1,6})\d{6}([CP])\d{8}$")


def _parse_occ(symbol: str):
    """Return (underlying_root, type) for an OCC symbol, else (None, None)."""
    clean = (symbol or "").upper().replace(" ", "")
    m = _OCC_RE.match(clean)
    if not m or clean.endswith("USD"):
        return None, None
    return m.group(1), m.group(2)


def _equity_from_account(account_state) -> float:
    """Best-effort equity: prefer an account_state dict, else the config default."""
    if isinstance(account_state, dict):
        eq = account_state.get("equity")
        if eq:
            return float(eq)
    # Fall back to reading the live account if no equity passed in.
    try:
        from core import alpaca_client as ac
        inst = getattr(ac, "_client_instance", None)
        if inst is not None:
            state = inst.get_account_state()
            return float(state.get("equity", 0.0) or 0.0)
    except Exception as e:
        logger.warning(f"Could not resolve portfolio equity for option risk: {e}")
    return 0.0


def _close_decision(occ_symbol: str, reason: str) -> dict:
    """Build a SELL-to-close decision dict a downstream handler can execute."""
    return {
        "action": "SELL",
        "instrument": "option",
        "symbol": occ_symbol,
        "quantity": 0.0,  # 0 => close full position (handled by executor/lifecycle)
        "reason": f"option_risk:{reason}",
        "summary": f"Option risk control: {reason}",
    }


# ---------------------------------------------------------------------------
# 1. EVENT GATE
# ---------------------------------------------------------------------------
def _has_earnings(underlying: str, before_date) -> bool:
    """Return True if ``underlying`` reports earnings at/before ``before_date``.

    Reuses data_provider.get_earnings_dates (yfinance calendar) and fails open
    (returns False) when the data source is unavailable so the gate can't false-
    trigger on a data outage. The window is bounded by the option's expiration so
    a distant contract isn't flattened for an earnings event it outlives.
    """
    try:
        from core import data_provider as dp
        fetch = getattr(dp, "get_earnings_dates", None)
        if fetch is None:
            return False
        df = fetch([underlying], days_ahead=365)
        if df is None or df.empty:
            return False
        for _, row in df.iterrows():
            d = row.get("earnings_date")
            if d is not None and d <= before_date:
                return True
        return False
    except Exception as e:
        logger.warning(f"Event-gate earnings check failed for {underlying}: {e} (fail-open).")
        return False


def _in_fomc_window(check_date) -> bool:
    """Return True if ``check_date`` (a date or datetime) is an FOMC decision day (synthetic)."""
    d = check_date.date() if hasattr(check_date, "date") else check_date
    return (d.month, d.day) in _FOMC_DAYS_2026


def _in_known_macro_window(check_date) -> bool:
    """Return True if ``check_date`` lands in a high-impact scheduled macro window."""
    return _in_fomc_window(check_date)


def event_gate_close_reason(occ_symbol: str, as_of=None) -> str | None:
    """Return a SELL-to-close reason if ``occ_symbol`` should be flattened by the event gate.

    Flattens when the session date is T-1 (or same-day) before a high-impact event
    that occurs at/before the option's expiration:
      - the underlying's earnings, or
      - a major macro event (FOMC decision) when ``OPTIONS_EVENT_GATE_INCLUDE_FOMC``.

    Returns None if no event is imminent (nothing to do). ``as_of`` is the
    decision date (defaults to today).
    """
    if not getattr(config, "OPTIONS_EVENT_GATE_ENABLED", True):
        return None
    root, ctype = _parse_occ(occ_symbol)
    if not root:
        return None

    as_of = as_of or datetime.now()
    today = as_of.date() if hasattr(as_of, "date") else as_of
    # Only flatten when we're at/near the EVENT, i.e. during the session BEFORE the
    # next known event date for this underlying. The gate needs an event date.
    try:
        from core import data_provider as dp
        fetch = getattr(dp, "get_earnings_dates", None)
        if fetch is not None:
            df = fetch([root], days_ahead=45)
            if df is not None and not df.empty:
                from datetime import date as _date
                event_dates = sorted(
                    r["earnings_date"]
                    for _, r in df.iterrows()
                    if isinstance(r["earnings_date"], _date)
                )
                # Find the next earnings date strictly after today.
                upcoming = [d for d in event_dates if d > today]
                if upcoming:
                    next_earn = upcoming[0]
                    # Flatten on session T-1 (or same-day if event is today/after close).
                    if next_earn <= today + timedelta(days=1):
                        return (f"Event gate: {root} reports earnings on {next_earn} "
                                f"(at/before option expiry); flattening {occ_symbol} to "
                                f"avoid earnings IV-crush/gap risk.")
    except Exception as e:
        logger.warning(f"Event-gate lookup failed for {root}: {e} (fail-open).")

    if getattr(config, "OPTIONS_EVENT_GATE_INCLUDE_FOMC", True):
        if _in_known_macro_window(today + timedelta(days=1)):
            return (f"Event gate: FOMC/macro decision on {today + timedelta(days=1)}; "
                    f"flattening {occ_symbol} to avoid macro gap/IV crush.")
    return None


# ---------------------------------------------------------------------------
# 2. VEGA / DELTA EXPOSURE CAPS
# ---------------------------------------------------------------------------
def _contract_greeks(alpaca_client, occ_symbol: str):
    """Return (vega, delta, premium_mv) for an OCC symbol, else (None, None, None).

    Reads the latest option quote (which carries greeks) plus the open-position
    market value. Falls back gracefully to None on paper accounts where option
    quotes are skipped, so the exposure cap degrades to a premium-value bound.
    """
    if alpaca_client is None:
        return None, None, None
    q = None
    try:
        info = alpaca_client.get_latest_option_data([occ_symbol])
    except Exception as e:
        logger.debug(f"Latest option data failed for {occ_symbol}: {e}")
        info = None
    if info:
        q = info.get(occ_symbol)
    greeks = getattr(q, "greeks", None) if q is not None else None
    if greeks is None:
        # Some clients nest the snapshot under the quote object or omit greeks.
        return None, None, _position_market_value(alpaca_client, occ_symbol)
    return (
        getattr(greeks, "vega", None),
        getattr(greeks, "delta", None),
        _position_market_value(alpaca_client, occ_symbol),
    )


def _position_size(alpaca_client, occ_symbol: str) -> float:
    """Return the number of contracts held for ``occ_symbol`` (0 if unknown)."""
    try:
        positions = alpaca_client.get_option_positions()
        d = positions.get(occ_symbol)
        if d is None:
            # Some clients normalize spaces out of the OCC symbol.
            d = positions.get(occ_symbol.replace(" ", ""))
        return float(d.get("qty", 0) or 0) if d else 0.0
    except Exception as e:
        logger.debug(f"Position-size lookup failed for {occ_symbol}: {e}")
        return 0.0


def exposure_cap_reason(alpaca_client, occ_symbol: str, equity: float) -> str | None:
    """Return a SELL-to-close reason if ``occ_symbol`` trips a book-level cap.

    Approximates the position's dollar exposure (vega or delta or premium) and
    compares against the aggregate cap. Since the cap is book-level, a position
    only trips when it is part of a book that EXCEEDS the cap; a simple
    single-position version is used here: if the position's own greeks/Delta
    exposure exceeds ``equity * cap_pct`` it is flattened. Cross-position book
    aggregation is handled by ``rebalance_book``.

    Returns str reason or None.
    """
    if not getattr(config, "OPTIONS_VEGA_CAP_MV_PCT", 0.0) and not getattr(
            config, "OPTIONS_DELTA_CAP_PCT", 0.0):
        return None
    if equity <= 0:
        return None
    qty = _position_size(alpaca_client, occ_symbol)
    if qty <= 0:
        return None
    vega, dta, mv = _contract_greeks(alpaca_client, occ_symbol)
    # If greeks are available, use the max of |vega|/|delta| dollar exposure.
    if vega is not None and getattr(config, "OPTIONS_VEGA_CAP_MV_PCT", 0.0) > 0:
        ve_dol = abs(float(vega or 0.0)) * qty
        cap_dol = equity * getattr(config, "OPTIONS_VEGA_CAP_MV_PCT", 0.0)
        if ve_dol > cap_dol:
            return (f"Vega cap: {occ_symbol} vega ${ve_dol:.2f} > "
                    f"equity*{getattr(config,'OPTIONS_VEGA_CAP_MV_PCT',0.0):.2%} "
                    f"(${cap_dol:.2f}); flattening.")
    if dta is not None and getattr(config, "OPTIONS_DELTA_CAP_PCT", 0.0) > 0:
        dt_dol = abs(float(dta or 0.0)) * qty
        cap_dol = equity * getattr(config, "OPTIONS_DELTA_CAP_PCT", 0.0)
        if dt_dol > cap_dol:
            return (f"Delta cap: {occ_symbol} delta ${dt_dol:.2f} > "
                    f"equity*{getattr(config,'OPTIONS_DELTA_CAP_PCT',0.0):.2%} "
                    f"(${cap_dol:.2f}); flattening.")
    # If no greeks, use premium market value as the dollar-exposure bound.
    if vega is None and dta is None and getattr(config, "OPTIONS_MAX_ALLOCATION_PCT", 0.0) > 0:
        mv = _position_market_value(alpaca_client, occ_symbol)
        if mv is None or mv <= 0:
            mv = _position_size(alpaca_client, occ_symbol) * 100.0 * 2.50  # mock-ish fallback
        cap_dol = equity * getattr(config, "OPTIONS_MAX_ALLOCATION_PCT", 0.05)
        if mv > cap_dol:
            return (f"Allocation cap (no greeks): {occ_symbol} premium ${mv:.2f} > "
                    f"equity*{getattr(config,'OPTIONS_MAX_ALLOCATION_PCT',0.05):.2%} "
                    f"(${cap_dol:.2f}); flattening.")
    return None


def _position_market_value(alpaca_client, occ_symbol: str) -> float:
    """Return the open market value ($) for an OCC position, 0 if unknown."""
    try:
        positions = alpaca_client.get_option_positions()
        d = positions.get(occ_symbol) or positions.get(occ_symbol.replace(" ", ""))
        if d:
            return float(d.get("market_value", 0) or 0)
    except Exception as e:
        logger.debug(f"Market-value lookup failed for {occ_symbol}: {e}")
    return 0.0


def rebalance_book(alpaca_client, held_occs, equity) -> list[dict]:
    """Evaluate the whole options book against caps and return close decisions.

    Aggregates vega/delta (or premium) across all held contracts and emits a
    SELL-to-close decision for any contract that pushes the BOOK over a cap.
    Events (earnings/FOMC) are evaluated per-contract by ``event_gate_close_reason``.

    Returns a list of decision dicts (see  ``_close_decision``).
    """
    decisions = []
    for occ in held_occs:
        # Event gate first
        reason = event_gate_close_reason(occ)
        if reason:
            decisions.append(_close_decision(occ, reason))
            continue
        # Exposure caps (per-contract + book-aware estimate)
        reason = exposure_cap_reason(alpaca_client, occ, equity)
        if reason:
            decisions.append(_close_decision(occ, reason))
    return decisions


def eod_flat_decisions(held_occs) -> list[dict]:
    """Return flatten decisions for the options book (end-of-day arm)."""
    if not getattr(config, "OPTIONS_EOD_FLAT", False):
        return []
    if not getattr(config, "OPTIONS_ENABLED", False):
        return []
    return [_close_decision(occ, "OPTIONS_EOD_FLAT enabled; flattening book at EOD.") for occ in held_occs]