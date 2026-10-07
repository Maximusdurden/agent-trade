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
    "META": {"entry_premium": 4.50, "delta": 0.45},
    # AMD/COIN/AMZN/PLTR failed the PF >= 1.50 gate in the Phase 3 screen and
    # are excluded from the active universe. Add them back only if they pass.
}
# Active universe = tickers retained above.
ACTIVE_UNIVERSE = list(TICKER_CONFIG.keys())

# Intraday evaluation loop cadence.
# Set to 5s to reduce REST API load (was 2s -> 60-120 req/min, triggering 429s).
EVAL_INTERVAL_SECONDS = 5

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
    """Fetch 1-min bars for all symbols in ONE batched request.

    ``AlpacaClient.get_historical_bars`` accepts a list of symbols and issues a
    single ``StockBarsRequest(symbol_or_symbols=[...])`` (with retry + per-symbol
    fallback), returning a MultiIndex frame. This replaces the serial
    ``_load_intraday`` per-ticker loop, cutting PM-scan latency from ~N round
    trips to 1.

    Falls back to per-symbol ``_load_intraday`` fetches when the batch call
    returns no usable intraday data (e.g. a client that only implements the
    paginated single-symbol path, or a transient batch failure) so the PM gate
    still evaluates every ticker.
    """
    if not symbols:
        return {}
    try:
            # Fetch enough 1-min bars to cover the full pre-market window (04:00 ET)
            # plus the prior session. A single trading day has ~390 1-min bars
            # (04:00-20:00 ET); limit=days_back*10 (50) only covered ~50 minutes,
            # which at 09:29 AM ET returned YESTERDAY's close bars, not today's
            # pre-market — silently disarming every ticker (PM range 0.0000%).
            df = client.get_historical_bars(
                symbols, limit=days_back * 390, timeframe_str=INTRADAY_INTERVAL)
    except Exception as e:
        logger.warning(f"Batch intraday fetch failed ({e}); falling back to "
                       f"per-symbol fetches.")
        df = None
    if df is None or df.empty:
        # Fallback: per-symbol paginated fetch (matches the pre-batch behavior).
        return {sym: _load_intraday(client, sym, days_back=days_back)
                for sym in symbols}
    if not isinstance(df.index, pd.MultiIndex):
            # Single-symbol result: wrap it. Validate the bars are intraday (the
            # caller may have returned daily bars for a 1min request); if not,
            # fall back to the per-symbol paginated fetch.
            sym = symbols[0]
            idx = pd.to_datetime(df.index)
            intraday_like = len(idx) > 1 and (idx[1] - idx[0]) < pd.Timedelta(hours=1)
            if not intraday_like:
                return {sym: _load_intraday(client, sym, days_back=days_back)}
            return {sym: df}
    out = {}
    for sym in symbols:
        try:
            sub = df.xs(sym.upper(), level=0)
        except KeyError:
            continue
        sub = sub.copy()
        sub.index = pd.to_datetime(sub.index)
        if sub.index.tzinfo is None:
            sub.index = sub.index.tz_localize("UTC")
        sub.index = sub.index.tz_convert(ET)
        out[sym] = sub.sort_index()
    # If the batch produced nothing usable (e.g. daily bars returned for a 1min
    # request), fall back to per-symbol fetches so the gate still runs.
    if not out:
        return {sym: _load_intraday(client, sym, days_back=days_back)
                for sym in symbols}
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
        df = t.history(period="1d", interval="1m", prepost=True)
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
        day = pd.Timestamp(session_date, tz=ET)
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
    if (now_et - latest_ts).total_seconds() > 120:
        return None

    high = float(latest_bar["high"])
    low = float(latest_bar["low"])
    close = float(latest_bar["close"])
    vwap = _vwap_at(vwap_series, latest_ts)

    # Bearish sweep: High > PMH, Close < PMH, Close < VWAP.
    if high > pmh and close < pmh and vwap is not None and close < vwap:
        return {"direction": "BEARISH", "entry_ts": latest_ts, "entry_price": close}

    # Bullish sweep: Low < PML, Close > PML, Close > VWAP.
    if low < pml and close > pml and vwap is not None and close > vwap:
        return {"direction": "BULLISH", "entry_ts": latest_ts, "entry_price": close}

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
                    status = str(getattr(updated, "status", "")).lower()
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

        if fill_price is None:
            fill_price = ask

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
    # sub-sector/beta pair (e.g. QQQ+SPY or NVDA+AMD).
    kept = {}
    for symbol, pm in armed.items():
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
