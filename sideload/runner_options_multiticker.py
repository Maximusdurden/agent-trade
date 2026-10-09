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
        SWEEP_MIN_PENETRATION,
        MODEL_A_START,
        MODEL_A_END,
        POLL_INTERVAL_SECONDS,
            INTRADAY_INTERVAL,
        )
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message
from core.database import record_rejection

logger = logging.getLogger("RunnerOptionsMultiTicker")

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Universe config (TSLA + approved candidates from Phase 3 screen).
# ---------------------------------------------------------------------------
# entry_premium / delta per ticker. Only tickers with PF >= 1.50 are retained.
TICKER_CONFIG = {
    "TSLA": {"entry_premium": 3.50, "delta": 0.45},
    # META disabled 2026-10-07: corrected backtest model (ticker-specific
    # premiums + capped target fills) shows META PF 0.91 (negative expectancy)
    # under live exit rules. Re-enable only if a re-screen clears PF >= 1.50.
    # "META": {"entry_premium": 4.50, "delta": 0.45},
    # AMD/COIN/AMZN/PLTR failed the PF >= 1.50 gate in the Phase 3 screen and
    # are excluded from the active universe. Add them back only if they pass.
}
# Active universe = tickers retained above.
ACTIVE_UNIVERSE = list(TICKER_CONFIG.keys())

# Intraday evaluation loop cadence.
# Set to 5s to reduce REST API load (was 2s -> 60-120 req/min, triggering 429s).
EVAL_INTERVAL_SECONDS = 5

# PM-gate boot target: the runner sleeps until this ET time when it boots early
# (Cloud Run cold start can take 3-4 min, so the scheduler fires at 09:25 ET and
# the container holds until the PM gate moment). This guarantees the poll loop
# is live from 09:30:00 ET — no missed setups from late cold starts.
PM_GATE_BOOT_TARGET = dtime(9, 29, 50)

# Entry fill confirmation.
FILL_CONFIRM_SECONDS = 5   # Max seconds to poll for a fill.
FILL_POLL_INTERVAL = 1.0   # Poll cadence (seconds).

# ---------------------------------------------------------------------------
# Scaled guardrails (Phase 4 expansion).
# ---------------------------------------------------------------------------
# Max concurrent open option positions across the account. When this ceiling is
# reached, no new entries are dispatched (all_disarmed).
MAX_CONCURRENT_OPTION_POSITIONS = int(
    os.environ.get("OPTIONS_MAX_CONCURRENT_POSITIONS", "3"))
# Max trades per day (was hard-capped at 1 by the circuit breaker). Relaxed to
# allow one entry per arming slot; stop-out still halts the engine for the day.
MAX_DAILY_OPTION_TRADES = int(os.environ.get("OPTIONS_MAX_DAILY_TRADES", "3"))
# Sub-sector / beta correlation mutex: never hold two positions in the same
# tracking pair simultaneously (e.g. QQQ+SPY or NVDA+AMD are the same exposure).
CORRELATION_PAIRS = [
    {"SPY", "QQQ"},
    {"NVDA", "AMD"},
]


def _batch_load_intraday(client: AlpacaClient, symbols: list[str],
                         days_back: int = 5) -> dict[str, pd.DataFrame]:
    """Fetch 1-min bars for all symbols (bounded window, no memory blowup).

    NOTE: ``get_historical_bars`` with a large ``limit`` is a memory bomb for
    1-min data: the 1min day_multiplier is 10, so ``limit=1950`` fetches
    ``1950*10 = 19,500`` calendar days of 1-min bars (~7.6M rows/symbol) before
    trimming to the last 1950 — OOM on the 1Gi Cloud Run container (observed
    2026-10-08: "Out-of-memory event detected in container", signal 9).

    Instead, fetch a bounded ``days_back`` window per symbol via the paginated
    client (no multiplier), which covers the pre-market window + prior session
    with ~5*390 = 1950 bars/symbol.
    """
    if not symbols:
        return {}
    out = {}
    for sym in symbols:
        try:
            df = client.get_historical_bars_paginated(
                sym, timeframe_str=INTRADAY_INTERVAL, days_back=days_back)
        except Exception as e:
            logger.warning(f"Batch intraday fetch failed for {sym} ({e}); "
                           f"falling back to per-symbol fetch.")
            df = None
        if df is None or df.empty:
            df = _load_intraday(client, sym, days_back=days_back)
        if df is None or df.empty:
            continue
        df = df.copy()
        # The paginated client returns a MultiIndex (symbol, timestamp) even
        # for a single symbol; drop the symbol level.
        if isinstance(df.index, pd.MultiIndex):
            df = df.reset_index(level=0, drop=True)
        df.index = pd.to_datetime(df.index)
        if df.index.tzinfo is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(ET)
        out[sym] = df.sort_index()
    return out


def _load_pm_bars_yfinance(symbol: str, session_date: str) -> pd.DataFrame:
    """Fetch today's pre-market (04:00-09:29 ET) 1-min bars via yfinance.

    Alpaca's IEX feed has NO pre-market bars and the SIP feed blocks recent
    queries on this subscription, so the PM gate cannot use Alpaca for today's
    pre-market data. yfinance (free, already a dependency) returns pre-market
    bars with ``prepost=True``. Fails open (empty frame) so the gate degrades
    gracefully if yfinance is unavailable.
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed; PM gate unavailable (fail-open).")
        return pd.DataFrame()

    try:
        t = yf.Ticker(symbol)
        # Explicit start/end bounds instead of period="1d": on Monday mornings
        # (or holiday schedules) period="1d" can return Friday's session, which
        # would be filtered out below and silently fail-open. Bounding the
        # request to the session date keeps the fetch aligned with the gate.
        day = pd.Timestamp(session_date, tz=ET)
        start_date = day.strftime("%Y-%m-%d")
        end_date = (day + timedelta(days=1)).strftime("%Y-%m-%d")
        df = t.history(start=start_date, end=end_date,
                       interval="1m", prepost=True)
        if df is None or df.empty:
            return pd.DataFrame()
        idx = pd.to_datetime(df.index)
        if idx.tzinfo is None:
            idx = idx.tz_localize("UTC")
        idx = idx.tz_convert(ET)
        df = df.copy()
        df.index = idx
        df = df.sort_index()

        # Keep only today's pre-market window (04:00-09:29 ET).
        pm_start = day.replace(hour=4, minute=0)
        pm_end = day.replace(hour=9, minute=29)
        pm_bars = df[(df.index >= pm_start) & (df.index <= pm_end)]
        # Normalize column names to the runner's expected schema.
        rename = {"High": "high", "Low": "low", "Open": "open",
                  "Close": "close", "Volume": "volume"}
        pm_bars = pm_bars.rename(columns=rename)
        keep = [c for c in ("open", "high", "low", "close", "volume")
                if c in pm_bars.columns]
        return pm_bars[keep]
    except Exception as e:
        logger.warning(f"yfinance PM fetch failed for {symbol}: {e} (fail-open).")
        return pd.DataFrame()


def _pm_volatility_for(client: AlpacaClient, symbol: str, session_date: str,
                       intraday: pd.DataFrame | None = None) -> dict:
    """Compute the PM volatility gate for a specific ticker.

    ``intraday`` may be pre-fetched via ``_batch_load_intraday`` (one batched
    request for the whole universe); when omitted, falls back to a per-symbol
    fetch for backward compatibility.

    Pre-market bars come from yfinance (Alpaca IEX has none; SIP blocks recent
    queries on this subscription). If yfinance returns no pre-market bars, the
    gate fails open with a clear reason rather than a bogus 0.0000% range.
    """
    if intraday is None:
        intraday = _load_intraday(client, symbol, days_back=5)
    if intraday.empty:
        return {"pass": False, "reason": "No intraday data", "pmh": None, "pml": None}

    day = pd.Timestamp(session_date, tz=ET)
    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    day_bars = intraday[(intraday.index >= day_start) & (intraday.index < day_end)]
    if day_bars.empty:
        return {"pass": False, "reason": "No bars on session date", "pmh": None, "pml": None}

    # Pre-market bars: prefer yfinance (free, has real 04:00-09:29 ET data).
    # Alpaca IEX returns at most a single flat pre-market print (bogus 0.0000%
    # range) and SIP blocks recent queries on this subscription, so Alpaca
    # cannot be the PM source. Fall back to Alpaca's window only if yfinance
    # is unavailable.
    pm_bars = _load_pm_bars_yfinance(symbol, session_date)
    if pm_bars.empty:
        pm_start = day_start.replace(hour=4, minute=0)
        pm_end = day_start.replace(hour=9, minute=29)
        pm_bars = day_bars[(day_bars.index >= pm_start) & (day_bars.index <= pm_end)]
        if pm_bars.empty:
            return {"pass": False, "reason": "No pre-market bars (yfinance + Alpaca)",
                    "pmh": None, "pml": None}

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


def _correlation_conflict(symbol: str, active_symbols: set[str]) -> str | None:
    """Return the conflicting symbol if ``symbol`` shares a correlation pair.

    Prevents simultaneous entries on identical market exposure (e.g. QQQ+SPY or
    NVDA+AMD). Returns None when no conflict.
    """
    for pair in CORRELATION_PAIRS:
        if symbol in pair:
            conflict = (pair & active_symbols) - {symbol}
            if conflict:
                return next(iter(conflict))
    return None


def _rank_armed_by_pm_range(armed: dict, max_slots: int) -> dict:
    """Keep only the top ``max_slots`` armed tickers by PM range magnitude.

    When more tickers clear the 0.20% PM gate than slots allow, the highest
    premarket expansion names win (priority allocation).
    """
    ranked = sorted(armed.items(),
                    key=lambda kv: kv[1].get("range_pct", 0.0), reverse=True)
    return dict(ranked[:max_slots])


def _model_a_setup_for(client: AlpacaClient, symbol: str, session_date: str,
                       pmh: float, pml: float) -> dict | None:
    """Detect a Model A setup for a specific ticker on 1-min bar close.

    Only completed 1-minute candles are evaluated (never the in-flight bar).
    Fetches only today's bars (days_back=1) to avoid REST API flooding.
    """
    # Fetch only today's bars (PM anchors already computed at 09:29:50).
    intraday = _load_intraday(client, symbol, days_back=1)
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

    # Defect 3: only evaluate strictly completed candles. The currently forming
    # 1-minute bar is excluded (its close is just the latest tick, not a close).
    now_et = datetime.now(ET)
    completed_cutoff = now_et.replace(second=0, microsecond=0)
    window = window[window.index < completed_cutoff]
    if window.empty:
        return None

    # Bar freshness: evaluate ONLY the most recently completed 1-minute bar.
    # Re-iterating from 09:30 every poll can re-trigger stale setups that
    # occurred minutes earlier (e.g. after a failed entry or a mid-session
    # reconnect). The entry must fire strictly on the bar that just closed.
    latest_ts = window.index[-1]
    latest_bar = window.iloc[-1]

    # Only trigger if the latest bar completed within the last 120 seconds.
    age_s = (now_et - latest_ts).total_seconds()
    if age_s > 120:
        logger.info(
            f"[{symbol}] Bar {latest_ts.strftime('%H:%M')} too stale "
            f"({age_s:.0f}s > 120s); no trigger."
        )
        return None

    high = float(latest_bar["high"])
    low = float(latest_bar["low"])
    close = float(latest_bar["close"])
    vwap = _vwap_at(vwap_series, latest_ts)

    # Minimum sweep penetration: the bar must push at least
    # SWEEP_MIN_PENETRATION dollars beyond PMH/PML to be a genuine liquidity
    # sweep, not a sub-cent noise wiggle (e.g. Oct 6's $0.03 false positive).
    min_pen = float(SWEEP_MIN_PENETRATION)

    # Bearish sweep: High > PMH + min_pen, Close < PMH, Close < VWAP.
    if (high - pmh) >= min_pen and close < pmh and vwap is not None and close < vwap:
        return {"direction": "BEARISH", "entry_ts": latest_ts, "entry_price": close}

    # Bullish sweep: Low < PML - min_pen, Close > PML, Close > VWAP.
    if (pml - low) >= min_pen and close > pml and vwap is not None and close > vwap:
        return {"direction": "BULLISH", "entry_ts": latest_ts, "entry_price": close}

    # Audit trail: explain WHY the latest bar did not trigger, so a missed
    # session is attributable to market conditions vs a software defect.
    swept_high = (high - pmh) >= min_pen
    swept_low = (pml - low) >= min_pen
    if swept_high or swept_low:
        reason_parts = []
        if swept_high:
            reason_parts.append(f"high {high:.2f} > PMH {pmh:.2f} (+{high-pmh:.2f})")
            if close >= pmh:
                reason_parts.append(f"close {close:.2f} NOT < PMH")
            if vwap is None:
                reason_parts.append("no VWAP")
            elif close >= vwap:
                reason_parts.append(f"close {close:.2f} NOT < VWAP {vwap:.2f}")
        if swept_low:
            reason_parts.append(f"low {low:.2f} < PML {pml:.2f} (-{pml-low:.2f})")
            if close <= pml:
                reason_parts.append(f"close {close:.2f} NOT > PML")
            if vwap is None:
                reason_parts.append("no VWAP")
            elif close <= vwap:
                reason_parts.append(f"close {close:.2f} NOT > VWAP {vwap:.2f}")
        logger.info(
            f"[{symbol}] Bar {latest_ts.strftime('%H:%M')} swept "
            f"({'/'.join(reason_parts)}); no trigger."
        )
    elif (high > pmh or low < pml):
        # Swept the anchor but penetration below the minimum threshold.
        pen_high = high - pmh if high > pmh else 0.0
        pen_low = pml - low if low < pml else 0.0
        logger.info(
            f"[{symbol}] Bar {latest_ts.strftime('%H:%M')} penetration too shallow "
            f"(high+{pen_high:.2f}/low-{pen_low:.2f} < min ${min_pen:.2f}); no trigger."
        )

    return None


def _enter_position(client: AlpacaClient, symbol: str, session_date: str,
                    setup: dict, dry_run: bool = False) -> dict | None:
    """Resolve expiry, select strike, size, and BUY the option for a ticker."""
    direction = setup["direction"]
    opt_type = "call" if direction == "BULLISH" else "put"
    cfg = TICKER_CONFIG[symbol]

    def _reject(stage: str, reason: str, details: dict | None = None) -> None:
        """Persist a rejection row to the shared DB (synced to GCS)."""
        record_rejection(
            lane="options_multiticker",
            session_date=session_date,
            symbol=symbol,
            stage=stage,
            reason=reason,
            details=details,
        )

    # 1. Resolve front-week expiry.
    expiry = resolve_front_week_expiry()
    logger.info(f"[{symbol}] Resolved front-week expiry: {expiry}")

    # 2. Select strike + size.
    strike_info = select_strike(client, symbol, direction, expiry,
                                current_price=setup["entry_price"])
    if not strike_info or not strike_info.get("selected_occ"):
        reason = f"No tradeable {opt_type} contract on {expiry}."
        logger.warning(f"[{symbol}] {reason}")
        _reject("strike_selection", reason, {"expiry": expiry, "direction": direction})
        return None

    occ = strike_info["selected_occ"]
    contracts = int(strike_info.get("contracts", 0))
    if contracts < 1:
        reason = strike_info.get("reject_reason") or f"Contract sizing rejected {occ} (contracts=0)."
        logger.warning(f"[{symbol}] {reason}")
        _reject("sizing", reason, {
            "occ": occ,
            "ask": strike_info.get("ask"),
            "bid": strike_info.get("bid"),
            "strike": strike_info.get("strike"),
            "delta": strike_info.get("delta"),
            "expiry": expiry,
            "direction": direction,
            "selection_note": strike_info.get("selection_note"),
        })
        return None

    ask = float(strike_info["ask"])
    # Spread gate.
    spread = check_spread(float(strike_info["bid"]), ask)
    if not spread["pass"]:
        reason = f"Spread gate failed for {occ}: {spread['reason']}"
        logger.warning(f"[{symbol}] {reason}")
        _reject("spread_gate", reason, {
            "occ": occ,
            "bid": strike_info.get("bid"),
            "ask": ask,
            "spread": spread.get("spread"),
            "rule": spread.get("rule"),
        })
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

        # Defect 4: confirm the limit order actually fills before tracking it.
        # place_option_order may return before the fill (status new/accepted) or
        # the order may be canceled if the ask moves. Poll up to 5s for a fill.
        order_id = result.get("id")
        status = str(result.get("status", "")).lower()
        filled_qty = contracts
        if order_id and status != "filled":
            logger.info(f"[{symbol}] Order {order_id} status={status}; "
                        f"polling up to {FILL_CONFIRM_SECONDS}s for fill...")
            for _ in range(int(FILL_CONFIRM_SECONDS / FILL_POLL_INTERVAL)):
                time.sleep(FILL_POLL_INTERVAL)
                try:
                    updated = client.trading_client.get_order_by_id(order_id=order_id)
                    status = str(getattr(updated, "status", "")).lower().replace("orderstatus.", "")
                    if getattr(updated, "filled_avg_price", None) is not None:
                        fill_price = float(updated.filled_avg_price)
                    fq = getattr(updated, "filled_qty", None)
                    if fq is not None:
                        filled_qty = int(float(fq))
                    if status == "filled":
                        break
                except Exception as poll_err:
                    logger.warning(f"[{symbol}] Fill poll error for {order_id}: {poll_err}")

        if status not in ("filled", "partially_filled"):
                    reason = (f"Entry order {order_id} not filled after "
                              f"{FILL_CONFIRM_SECONDS}s (status={status}); canceled.")
                    logger.error(f"[{symbol}] {reason}")
                    try:
                        if order_id:
                            client.trading_client.cancel_order_by_id(order_id=order_id)
                    except Exception as cancel_err:
                        logger.warning(f"[{symbol}] Cancel failed for {order_id}: {cancel_err}")
                    _reject("order_fill", reason, {
                        "occ": occ,
                        "contracts": contracts,
                        "order_id": order_id,
                        "status": status,
                        "ask": ask,
                    })
                    return None

        if fill_price is None:
            fill_price = ask
        # Track the actually-filled qty (a partial fill leaves fewer contracts
        # than requested; tracking the full qty would over-sell on exit).
        contracts = filled_qty if filled_qty >= 1 else contracts

    pos = {
        "contract_symbol": occ,
        "entry_time": datetime.now(ET).isoformat(),
        "entry_premium": fill_price,
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

    # Pull the shared DB from GCS so rejection rows merge into the freshest
    # snapshot (and so upload_to_gcs() has a local DB to work with). The merge
    # logic in upload_to_gcs() preserves rows from other lanes.
    try:
        from core.gcs_sync import download_from_gcs
        download_from_gcs()
    except Exception as dl_err:
        logger.warning(f"[GCS] DB download failed: {dl_err}")

    try:
        return _run_session_inner(client, state, session_date, dry_run, active)
    finally:
        # Persist the DB back to GCS so rejection rows survive the ephemeral
        # container. The merge logic preserves rows from other lanes.
        try:
            from core.gcs_sync import upload_to_gcs
            upload_to_gcs()
        except Exception as up_err:
            logger.warning(f"[GCS] DB upload failed: {up_err}")


def run_replay_session(replay_date: str) -> dict:
    """Replay a historical session offline with a synthetic clock.

    Runs the SAME code path as the live session (run_session -> _run_session_inner)
    but with ``datetime.now(ET)`` replaced by a simulated clock that ticks from
    09:29:50 to 10:15:00 ET in 5-second increments, feeding each poll only the
    1-minute bars that would have been visible at that simulated time (no
    lookahead). Alpaca order endpoints are mocked so no real orders are placed.

    This decouples regression testing from the live 09:30-10:15 ET window: a bug
    that costs a 24-hour iteration loop in production is caught offline here.

    Usage:
        python -m sideload.runner_options_multiticker --replay-date 2026-10-08
    """
    import datetime as _dt
    from unittest import mock

    logger.info(f"=== Replay session {replay_date} (offline, synthetic clock) ===")
    real_client = AlpacaClient()
    state = {"active_position": None, "last_session": replay_date}

    # Reset circuit-breaker + position state so the replay is idempotent
    # (repeated runs must not accumulate trades_today and get "skipped").
    from sideload.options_execution_guards import STATE_FILE as CB_STATE_FILE
    for path in (CB_STATE_FILE, STATE_PATH):
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                logger.warning(f"Could not reset {path}: {e}")

    class _Clock:
        def __init__(self, start: _dt.datetime):
            self.t = start

        def now(self, tz=None):
            return self.t

        def advance(self, **kw):
            self.t = self.t + _dt.timedelta(**kw)

    class _FakeDatetime(_dt.datetime):
        _clock = None

        @classmethod
        def now(cls, tz=None):
            return cls._clock.now(tz)

    # ---- Preload the replay date's real bars ONCE (network calls) ----
    # Then the in-memory client slices them by simulated time on every poll,
    # so the 45-minute window replays in seconds with NO lookahead bias.
    day = pd.Timestamp(replay_date, tz=ET)
    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    replay_bars: dict[str, pd.DataFrame] = {}
    for sym in ACTIVE_UNIVERSE:
        try:
            df = real_client.get_historical_bars_paginated(
                sym, timeframe_str=INTRADAY_INTERVAL, days_back=1)
        except Exception:
            df = None
        if df is None or df.empty:
            df = _load_intraday(real_client, sym, days_back=1)
        if df is None or df.empty:
            continue
        df = df.copy()
        if isinstance(df.index, pd.MultiIndex):
            df = df.reset_index(level=0, drop=True)
        df.index = pd.to_datetime(df.index)
        if df.index.tzinfo is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(ET)
        replay_bars[sym] = df.sort_index()
    logger.info(f"Replay data: {list(replay_bars.keys())} "
                f"({len(replay_bars.get('TSLA', pd.DataFrame()))} TSLA bars)")

    # ---- In-memory client that serves only bars visible at simulated time ----
    class _ReplayClient:
        """Mimics the AlpacaClient surface the runner uses, no live orders."""

        trading_client = type("T", (), {"cancel_order_by_id": lambda self, o=None: None})()

        def __init__(self, bars: dict[str, pd.DataFrame]):
            self._bars = bars
            self.entry_calls = 0

        def _visible(self, symbol: str) -> pd.DataFrame:
            df = self._bars.get(symbol, pd.DataFrame())
            if df.empty:
                return df
            # No lookahead: only bars with close time <= simulated clock time.
            now_t = _FakeDatetime.now(ET)
            return df[df.index <= now_t]

        def get_historical_bars_paginated(self, symbol, timeframe_str="1min",
                                          days_back=5):
            return self._visible(symbol)

        def get_historical_bars(self, symbol, limit=100, timeframe_str="day",
                                max_retries=3):
            return pd.DataFrame()

        def get_option_chain_snapshot(self, underlying_symbol, expiration_date_gte=None,
                                      expiration_date_lte=None, strike_price_gte=None,
                                      strike_price_lte=None, contract_type=None):
            # No lookahead on the chain either: serve today's LIVE chain (real
            # quotes are read-only; entry uses the ask from the real market).
            return real_client.get_option_chain_snapshot(
                underlying_symbol=underlying_symbol,
                expiration_date_gte=expiration_date_gte,
                expiration_date_lte=expiration_date_lte,
                strike_price_gte=strike_price_gte,
                strike_price_lte=strike_price_lte,
                contract_type=contract_type)

        def get_latest_price(self, symbol):
            df = self._visible(symbol)
            if df.empty:
                return real_client.get_latest_price(symbol)
            return float(df.iloc[-1]["close"])

        def get_latest_option_data(self, symbols):
            # Serve real chain quotes (bid/ask) so the monitor loop can
            # evaluate take-profit/stop in dry-run (no live orders).
            try:
                from sideload.options_strike_sizer import _parse_occ
                occ = symbols[0] if isinstance(symbols, list) else symbols
                parsed = _parse_occ(occ)
                if not parsed:
                    return {}
                opt_type = "put" if parsed["type"] == "PUT" else "call"
                chain = real_client.get_option_chain_snapshot(
                    underlying_symbol=parsed["root"],
                    expiration_date_gte=None, expiration_date_lte=None,
                    contract_type=opt_type)
                for o, snap in chain.items():
                    if o.upper() == occ.upper():
                        quote = getattr(snap, "latest_quote", None)
                        if quote is not None:
                            return {occ: quote}  # has bid_price/ask_price attrs
            except Exception:
                pass
            return {}

        def place_option_order(self, symbol, qty, side, limit_price=None,
                               client_order_id=None):
            self.entry_calls += 1
            return {"id": f"replay-{int(_dt.datetime.now().timestamp())}",
                    "symbol": symbol, "qty": qty, "side": side,
                    "filled_avg_price": float(limit_price or 2.50),
                    "status": "filled"}

        def close_option_position(self, symbol):
            return {"id": f"replay-close-{int(_dt.datetime.now().timestamp())}",
                    "symbol": symbol, "qty": 1, "side": "sell",
                    "filled_avg_price": 2.50, "status": "filled"}

        def get_option_positions(self):
            return {}

    client = _ReplayClient(replay_bars)

    # GCS sync is skipped in replay (no upload of synthetic results).
    clock = _Clock(_dt.datetime(
        _dt.datetime.strptime(replay_date, "%Y-%m-%d").year,
        _dt.datetime.strptime(replay_date, "%Y-%m-%d").month,
        _dt.datetime.strptime(replay_date, "%Y-%m-%d").day,
        9, 29, 50, tzinfo=ET))
    _FakeDatetime._clock = clock
    with mock.patch.object(sys.modules[__name__], "datetime", _FakeDatetime), \
         mock.patch.object(sys.modules["sideload.runner_options_tsla"],
                           "datetime", _FakeDatetime), \
         mock.patch.object(sys.modules[__name__], "time") as mock_mt_time, \
         mock.patch.object(sys.modules["sideload.runner_options_tsla"],
                           "time") as mock_tsla_time, \
         mock.patch.object(sys.modules[__name__], "AlpacaClient",
                           return_value=client):
        # Each sleep in the poll/monitor loops advances the synthetic clock, so
        # the loop walks the FULL 09:30-10:15 ET window (and the monitor's
        # 30-min hold) instantly instead of in real time.
        mock_mt_time.sleep = lambda s: clock.advance(seconds=int(s))
        mock_tsla_time.sleep = lambda s: clock.advance(seconds=int(s))
        try:
            result = _run_session_inner(client, state, replay_date, dry_run=True,
                                        active=ACTIVE_UNIVERSE)
        finally:
            pass
    result["replay"] = True
    result["replay_entry_calls"] = client.entry_calls
    return result


def _run_session_inner(client: AlpacaClient, state: dict, session_date: str,
                       dry_run: bool, active: list[str]) -> dict:
    """Core session logic (wrapped by run_session for GCS DB sync)."""
    # Defect 1: crash recovery. If an active position exists on boot (container
    # restarted/redeployed while a position was open), re-attach directly to the
    # monitoring loop BEFORE the circuit breaker (which would otherwise see a
    # recorded trade and skip, orphaning the position).
    if state.get("active_position"):
        logger.warning("Found unclosed active position on startup; "
                       "re-attaching monitor loop...")
        pos = state["active_position"]
        exit_info = _monitor_position(client, pos, dry_run=dry_run)
        # Only clear state if the recovered exit actually filled. If the exit
        # order did not fill (take-profit limit unfilled, emergency market sell
        # failed), keep the active_position so a later run re-attaches and
        # closes it — otherwise the broker position is orphaned.
        if exit_info.get("closed", True):
            state["active_position"] = None
            save_state(state)
            sync_up_to_gcs()
        # Record the recovered trade so the circuit breaker (max 1 trade/day +
        # halt-on-stop-out) is respected even after a container restart.
        stopped_out = str(exit_info.get("reason", "")).startswith("stop_loss")
        record_trade(session_date, stopped_out=stopped_out)
        return {"status": "recovered_and_closed", "position": pos,
                "exit": exit_info}

    # Circuit breaker: max N trades/day (was 1; relaxed for multi-ticker).
    can_trade = check_can_trade(session_date, max_trades=MAX_DAILY_OPTION_TRADES)
    if not can_trade["can_trade"]:
        logger.info(f"Circuit breaker: {can_trade['reason']}")
        return {"status": "skipped", "reason": can_trade["reason"]}

    # Concurrency ceiling: block new entries when the account already holds
    # MAX_CONCURRENT_OPTION_POSITIONS open option positions.
    try:
        open_positions = client.get_option_positions()
        open_count = len(open_positions)
    except Exception as e:
        logger.warning(f"Could not fetch open option positions: {e}")
        open_count = 0
    if open_count >= MAX_CONCURRENT_OPTION_POSITIONS:
        logger.info(f"Concurrency ceiling reached: {open_count} open option "
                    f"positions >= {MAX_CONCURRENT_OPTION_POSITIONS}.")
        return {"status": "skipped",
                "reason": f"MAX_CONCURRENT_POSITIONS_REACHED ({open_count})"}

    # Macro blackout (no scheduled releases by default).
    blackout = check_macro_blackout(session_date)
    if blackout["blocked"]:
        logger.info(f"Macro blackout: {blackout['reason']}")
        return {"status": "skipped", "reason": blackout["reason"]}

    # Early-boot hold: Cloud Run cold start can take 3-4 min (observed
    # 2026-10-09: scheduler fired 09:29, app started 09:32:55 — the 09:31
    # sweep bar was already >120s old and silently dropped). The scheduler now
    # fires at 09:25 ET; if this container is up before the PM-gate moment,
    # hold here until 09:29:50 ET so the poll loop is live from 09:30:00.
    now_et = datetime.now(ET)
    boot_target = now_et.replace(hour=PM_GATE_BOOT_TARGET.hour,
                                 minute=PM_GATE_BOOT_TARGET.minute,
                                 second=PM_GATE_BOOT_TARGET.second,
                                 microsecond=0)
    # Wait only if today is the session date (never hold a stale run past
    # market hours) and we are before the target.
    today_str = now_et.strftime("%Y-%m-%d")
    if today_str == session_date and now_et < boot_target:
        logger.info(
            f"Container initialized at {now_et.strftime('%H:%M:%S')}. "
            f"Sleeping until PM gate at 09:29:50 ET..."
        )
        while True:
            now_et = datetime.now(ET)
            if now_et >= boot_target:
                break
            # Sleep in small slices so shutdown signals are handled promptly
            # (Cloud Run sends SIGTERM on job deletion/forced stop; a single
            # long sleep would block graceful shutdown).
            time.sleep(min(5.0, (boot_target - now_et).total_seconds()))
        logger.info(f"PM gate moment reached at {datetime.now(ET).strftime('%H:%M:%S')}.")

    # 1. Pre-market volatility gate for each ticker; disarm failures.
    #    Batch-fetch all 1-min bars in ONE request, then slice per symbol.
    armed = {}
    pm_results = {}
    batch = _batch_load_intraday(client, active, days_back=5)
    for symbol in active:
        pm = _pm_volatility_for(client, symbol, session_date,
                                intraday=batch.get(symbol))
        pm_results[symbol] = pm
        if pm["pass"]:
            armed[symbol] = pm
            logger.info(f"[{symbol}] PM gate PASS (range {pm['range_pct']}%).")
        else:
            logger.info(f"[{symbol}] PM gate DISARMED: {pm['reason']}")

    if not armed:
        return {"status": "all_disarmed", "pm_results": pm_results}

    # Correlation mutex: drop any armed ticker that duplicates an already-armed
    # sub-sector/beta pair (e.g. QQQ+SPY or NVDA+AMD). Sort by PM range FIRST so
    # the higher-expansion ticker wins the slot when a pair conflicts - without
    # this, the first symbol in ACTIVE_UNIVERSE order would claim the slot even
    # if its premarket range was far smaller than its pair-mate's.
    kept = {}
    for symbol, pm in sorted(armed.items(),
                             key=lambda kv: kv[1].get("range_pct", 0.0),
                             reverse=True):
        conflict = _correlation_conflict(symbol, set(kept.keys()))
        if conflict:
            logger.info(f"[{symbol}] Correlation mutex: skipping (conflicts "
                        f"with {conflict}).")
            pm_results[symbol] = {**pm, "pass": False,
                                  "reason": f"Correlation mutex vs {conflict}"}
            continue
        kept[symbol] = pm
    armed = kept

    # Priority allocation: if more tickers armed than slots, keep only the top
    # MAX_CONCURRENT_OPTION_POSITIONS by PM range magnitude.
    if len(armed) > MAX_CONCURRENT_OPTION_POSITIONS:
        dropped = set(armed.keys()) - set(
            _rank_armed_by_pm_range(armed, MAX_CONCURRENT_OPTION_POSITIONS).keys())
        for symbol in dropped:
            logger.info(f"[{symbol}] Priority allocation: dropped (PM range "
                        f"{armed[symbol]['range_pct']}% below top "
                        f"{MAX_CONCURRENT_OPTION_POSITIONS}).")
            pm_results[symbol] = {**armed[symbol], "pass": False,
                                  "reason": "Priority allocation (PM range)"}
        armed = _rank_armed_by_pm_range(armed, MAX_CONCURRENT_OPTION_POSITIONS)

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

    # 6. Clear state ONLY if the exit actually filled. If the exit order did not
    #    fill (take-profit limit unfilled, emergency market sell failed), keep
    #    the active_position so a later run re-attaches and closes it.
    if exit_info.get("closed", True):
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
    parser.add_argument("--replay-date", default=None,
                        help="Replay a HISTORICAL session date YYYY-MM-DD using a "
                             "synthetic clock (09:29:50->10:15 ET) against that day's "
                             "actual bars, in dry-run mode with mocked order endpoints. "
                             "Use for offline regression testing without waiting for "
                             "the live market window.")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord notification.")
    args = parser.parse_args()

    dry_run = not args.live
    setup_jira_logging(app_name="agent-trade-sideload")
    # Ensure INFO audit lines (PM gate results, sweep/no-trigger explanations,
    # stale-bar drops) reach stderr so the Cloud Run logs are diagnosable. The
    # Jira handler only forwards ERROR/CRITICAL; without a stream handler the
    # audit trail is silent.
    _root = logging.getLogger()
    if not any(getattr(h, "stream", None) is not None for h in _root.handlers):
        _stream = logging.StreamHandler(sys.stderr)
        _stream.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s"))
        _root.addHandler(_stream)
    _root.setLevel(logging.INFO)
    try:
        if args.replay_date:
            result = run_replay_session(args.replay_date)
        else:
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
