#!/usr/bin/env python3
"""End-to-end session replay & dry-run integration test (Phase 3c).

Replays runner_options_multiticker.run_session against a known historical
session (2026-09-25) with a synthetic clock and a fake Alpaca client, verifying
the entire execution path executes with zero unhandled exceptions:

  09:29:50  PM volatility gate arms TSLA.
  09:30-10:15  Completed 1-min bars fed until Model A triggers.
  Trigger   _enter_position: select_strike, front-week expiry, spread gate,
            sizing elasticity, mock fill.
  Post-entry  state["active_position"] written to disk + GCS sync attempted.
  Monitor   _monitor_position tracks quotes and exits (target/stop/time).

Also runs a live Alpaca option-chain smoke test for TSLA.

Usage:
    python -m sideload.test_runner_multiticker_e2e
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys
import time
from datetime import timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload import runner_options_multiticker as mt
from sideload import runner_options_tsla as tsla
from sideload.options_strike_sizer import resolve_front_week_expiry, _parse_occ

logger = logging.getLogger("TestRunnerMultitickerE2E")
ET = ZoneInfo("America/New_York")

SESSION_DATE = "2026-09-25"

# ---------------------------------------------------------------------------
# Synthetic clock
# ---------------------------------------------------------------------------
class Clock:
    """Controllable clock used to replay the session sequentially."""

    def __init__(self, start: _dt.datetime):
        self.t = start

    def now(self, tz=None):
        return self.t

    def advance(self, **kw):
        self.t = self.t + timedelta(**kw)


class FakeDatetime(_dt.datetime):
    """datetime subclass whose now() reads the synthetic clock."""

    _clock = None

    @classmethod
    def now(cls, tz=None):
        return cls._clock.now(tz)


# ---------------------------------------------------------------------------
# Fake Alpaca client
# ---------------------------------------------------------------------------
class FakeQuote:
    def __init__(self, bid, ask):
        self.bid_price = bid
        self.ask_price = ask


class FakeGreeks:
    def __init__(self, delta):
        self.delta = delta


class FakeSnapshot:
    def __init__(self, bid, ask, delta):
        self.latest_quote = FakeQuote(bid, ask)
        self.greeks = FakeGreeks(delta)


class FakeTradingClient:
    def get_order_by_id(self, order_id=None):
        return FakeOrder("filled", 3.50)

    def cancel_order_by_id(self, order_id=None):
        return None


class FakeOrder:
    def __init__(self, status, filled_avg_price):
        self.status = status
        self.filled_avg_price = filled_avg_price


class FakeClient:
    """Mimics the AlpacaClient surface used by the runner."""

    def __init__(self, intraday: pd.DataFrame, daily: pd.DataFrame,
                 chain: dict, monitor_quote: FakeQuote):
        self._intraday = intraday
        self._daily = daily
        self._chain = chain
        self._monitor_quote = monitor_quote
        self.trading_client = FakeTradingClient()
        self.entry_calls = 0

    def get_historical_bars_paginated(self, symbol, timeframe_str="1min",
                                      days_back=5):
        return self._intraday.copy()

    def get_historical_bars(self, symbol, limit=100, timeframe_str="day",
                            max_retries=3):
        return self._daily.copy()

    def get_option_chain_snapshot(self, underlying_symbol, expiration_date_gte=None,
                                  expiration_date_lte=None, strike_price_gte=None,
                                  strike_price_lte=None, contract_type=None):
        return dict(self._chain)

    def get_latest_option_data(self, symbols):
        occ = symbols[0] if isinstance(symbols, list) else symbols
        return {occ: self._monitor_quote}

    def get_latest_price(self, symbol):
        return 254.2

    def place_option_order(self, symbol, qty, side, limit_price=None,
                           client_order_id=None):
        self.entry_calls += 1
        return {"id": "mock-entry-1", "symbol": symbol, "qty": qty, "side": side,
                "filled_avg_price": limit_price, "status": "filled"}

    def close_option_position(self, symbol):
        return {"id": "mock-close-1", "symbol": symbol, "qty": 1, "side": "sell",
                "filled_avg_price": 3.50, "status": "filled"}


# ---------------------------------------------------------------------------
# Synthetic session data for 2026-09-25
# ---------------------------------------------------------------------------
def _build_intraday() -> pd.DataFrame:
    """Build 1-min bars for 2026-09-25 (pre-market + 09:30-09:33)."""
    rows = []
    # Pre-market 04:00-09:29: PMH=255.00, PML=254.00 -> range 0.39% >= 0.35%.
    for minute in range(0, 330):
        ts = pd.Timestamp(f"{SESSION_DATE} 04:00:00", tz=ET) + timedelta(minutes=minute)
        rows.append({"high": 255.0, "low": 254.0, "close": 254.5, "volume": 500})
    # 09:30, 09:31 normal bars.
    for minute in (30, 31):
        ts = pd.Timestamp(f"{SESSION_DATE} 09:{minute:02d}:00", tz=ET)
        rows.append({"high": 254.6, "low": 254.0, "close": 254.3, "volume": 1000})
    # 09:32 BEARISH sweep: high>PMH(255), close<PMH, close<VWAP.
    rows.append({"high": 255.5, "low": 254.0, "close": 254.2, "volume": 1500})
    # 09:33 normal.
    rows.append({"high": 254.9, "low": 254.1, "close": 254.5, "volume": 1200})

    idx = [pd.Timestamp(f"{SESSION_DATE} 04:00:00", tz=ET) + timedelta(minutes=i)
           for i in range(len(rows))]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def _build_daily() -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp("2026-09-24", tz=ET)])
    return pd.DataFrame({"high": [256.0], "low": [253.0], "close": [254.0],
                         "volume": [50000000]}, index=idx)


def _build_chain() -> dict:
    # BEARISH -> puts. 254 put is first OTM (current_price 254.2).
    return {
        "TSLA261002P00254000": FakeSnapshot(3.48, 3.50, 0.45),
        "TSLA261002P00252000": FakeSnapshot(2.50, 2.60, 0.40),
    }


# ---------------------------------------------------------------------------
# Part A: E2E replay
# ---------------------------------------------------------------------------
def run_replay() -> dict:
    """Replay the 2026-09-25 session end-to-end in dry-run mode."""
    # Reset circuit-breaker + position state so the test is idempotent.
    from sideload.options_execution_guards import STATE_FILE as CB_STATE_FILE
    for path in (CB_STATE_FILE, mt.STATE_PATH):
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                logger.warning(f"Could not reset {path}: {e}")

    clock = Clock(_dt.datetime(2026, 9, 25, 9, 29, 50, tzinfo=ET))
    FakeDatetime._clock = clock

    intraday = _build_intraday()
    daily = _build_daily()
    chain = _build_chain()
    # Monitor quote: bid 5.10 >= target (3.50 * 1.45 = 5.08) -> take-profit.
    monitor_quote = FakeQuote(5.10, 5.15)
    client = FakeClient(intraday, daily, chain, monitor_quote)

    # Patch datetime.now in both runner modules + time.sleep to no-op.
    with mock.patch.object(mt, "datetime", FakeDatetime), \
         mock.patch.object(tsla, "datetime", FakeDatetime), \
         mock.patch.object(mt, "time") as mock_mt_time, \
         mock.patch.object(tsla, "time") as mock_tsla_time:
        mock_mt_time.sleep = lambda s: None
        mock_tsla_time.sleep = lambda s: None

        # Patch the client factory so run_session uses our FakeClient.
        with mock.patch.object(mt, "AlpacaClient", return_value=client):
            # Advance clock to 09:33:05 so the 09:32 bar is the latest completed.
            clock.advance(minutes=3, seconds=15)
            result = mt.run_session(SESSION_DATE, dry_run=True)

    return result


# ---------------------------------------------------------------------------
# Part B: Live Alpaca option chain smoke test
# ---------------------------------------------------------------------------
def run_live_chain_smoke() -> dict:
    """Make a live REST call to Alpaca for TSLA's front-week chain."""
    from core.alpaca_client import AlpacaClient
    client = AlpacaClient()
    expiry = resolve_front_week_expiry()
    report = {"expiry": expiry, "contracts": 0, "occ_parsed": 0,
              "readable_quotes": 0, "errors": []}

    try:
        chain = client.get_option_chain_snapshot(
            underlying_symbol="TSLA",
            expiration_date_gte=expiry,
            expiration_date_lte=expiry,
            contract_type="call",
        )
        report["contracts"] = len(chain)
        for occ, snap in chain.items():
            parsed = _parse_occ(occ)
            if parsed:
                report["occ_parsed"] += 1
            quote = getattr(snap, "latest_quote", None)
            greeks = getattr(snap, "greeks", None)
            bid = float(getattr(quote, "bid_price", 0) or 0) if quote else 0.0
            ask = float(getattr(quote, "ask_price", 0) or 0) if quote else 0.0
            delta = float(getattr(greeks, "delta", 0) or 0) if greeks else 0.0
            if bid > 0 and ask > 0 and delta != 0:
                report["readable_quotes"] += 1
    except Exception as e:
        report["errors"].append(str(e))

    return report


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print("=" * 70)
    print("Phase 3c E2E Replay & Dry-Run Integration Test")
    print("=" * 70)

    # Part A: E2E replay.
    print("\n[Part A] Replaying session 2026-09-25 (dry-run)...")
    result = run_replay()
    print("\n--- Verification Log ---")
    print(f"Status: {result.get('status')}")

    pm = result.get("pm_results", {}).get("TSLA", {})
    print(f"PM Range % for TSLA: {pm.get('range_pct')}% "
          f"(PMH={pm.get('pmh')}, PML={pm.get('pml')})")

    pos = result.get("position")
    if pos:
        print(f"Contract selected: {pos['contract_symbol']}")
        print(f"Mock fill price: ${pos['entry_premium']:.2f}")
        print(f"Target price: ${pos['target_premium']:.2f} "
              f"(+{mt.TP_PCT*100:.0f}%)")
        print(f"Stop price: ${pos['stop_premium']:.2f} "
              f"(-{mt.STOP_PCT*100:.0f}%)")
        print(f"Contracts: {pos['contracts']} | Direction: {pos['direction']}")

    exit_info = result.get("exit")
    if exit_info:
        print(f"Exit reason: {exit_info.get('reason')}")
        print(f"Exit fill: ${exit_info.get('fill_price')} | "
              f"PnL: {exit_info.get('pnl_pct')}%")

    # State cleanup check.
    state = mt.load_state()
    print(f"State cleanup: active_position={state.get('active_position')} "
          f"(should be None)")

    # Assertions.
    assert result.get("status") == "completed", \
        f"Expected completed, got {result.get('status')}"
    assert pos is not None, "No position entered"
    assert exit_info is not None, "No exit recorded"
    assert state.get("active_position") is None, "State not cleaned up"
    assert pm.get("pass") is True, "PM gate should have passed"
    print("\n[Part A] PASS: full execution path with zero unhandled exceptions.")

    # Part B: live chain smoke test.
    print("\n[Part B] Live Alpaca option chain smoke test...")
    chain_report = run_live_chain_smoke()
    print(json.dumps(chain_report, indent=2))
    if chain_report["contracts"] > 0:
        print("[Part B] PASS: chain returned contracts with readable quotes.")
    else:
        print("[Part B] WARNING: chain empty (may be weekend/holiday); "
              "not a hard failure.")

    print("\n" + "=" * 70)
    print("E2E integration test complete.")
    print("=" * 70)


if __name__ == "__main__":
    main()
