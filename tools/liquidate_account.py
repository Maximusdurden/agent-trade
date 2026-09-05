#!/usr/bin/env python3
"""Liquidate the Alpaca paper account (sell everything to cash).

STEP 1 of the migration cutover: before pointing agent-trade at the shared
dexter-trader paper account, sell ALL open positions (stocks, crypto, options)
so the account is a clean cash slate at the same balance.

THIS IS A DANGEROUS, POINT-OF-NO-RETURN OPERATION.
It is intentionally conservative:
  - DEFAULT is a dry-run that only lists what would be sold. Nothing is executed.
  - You must pass `--confirm` to actually place the liquidating sell orders.
  - `--yes` bypasses the interactive confirmation prompt (for scripted/CI use).

Usage:
    python -m tools.liquidate_account                     # dry-run: list positions
    python -m tools.liquidate_account --confirm           # interactive confirm + sell
    python -m tools.liquidate_account --confirm --yes     # sell without prompt

Notes:
    - Reads the account via agent-trade's AlpacaClient (uses ALPACA_* / paper).
    - At cutover this account is pointed at the dexter-trader paper credentials.
    - Prints a snapshot of holdings/equity BEFORE selling for the record.
"""

from __future__ import annotations

import argparse
import logging
import sys
import json
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import config
from core.alpaca_client import get_client_instance, ALPACA_AVAILABLE

logger = logging.getLogger("Liquidate")


def snapshot(client) -> dict:
    """Return a printable snapshot of account + positions (no side effects)."""
    account = client.get_account_state()
    positions = client.get_positions()
    return {
        "account": account,
        "positions": [
            {"symbol": s, "qty": p.get("qty"), "qty_available": p.get("qty_available"),
             "market_value": p.get("market_value"), "unrealized_pnl": p.get("unrealized_pnl"),
             "is_option": p.get("is_option", False)}
            for s, p in positions.items()
        ],
    }


def liquidate(client, simulate: bool = True) -> int:
    """Sell every open position to cash. Returns the number of sell orders placed.

    ``simulate=True`` only prints; ``simulate=False`` actually executes.
    """
    positions = client.get_positions()
    if not positions:
        print("[ok] No open positions. Account is already 100% cash.")
        return 0

    print(f"[i] Found {len(positions)} open position(s). Current book:")
    for s, p in positions.items():
        print(f"    {s}: qty={p.get('qty')} (avail {p.get('qty_available')}) "
              f"value=${p.get('market_value', 0):,.2f} unreal=${p.get('unrealized_pnl', 0):,.2f} "
              f"{'OPTION' if p.get('is_option') else ''}")

    placed = 0
    for s, p in positions.items():
        qty = p.get("qty_available") or p.get("qty") or 0.0
        if float(qty) <= 0:
            print(f"    [skip] {s}: qty_available=0, nothing sellable.")
            continue
        if p.get("is_option"):
            if simulate:
                print(f"    [plan] close_option_position({s})")
            else:
                try:
                    client.close_option_position(s)
                    print(f"    [ok] closed option {s}")
                    placed += 1
                except Exception as e:
                    print(f"    [err] close option {s}: {e}")
        else:
            if simulate:
                print(f"    [plan] execute_market_order({s}, qty={qty}, side='sell')")
            else:
                try:
                    res = client.execute_market_order(s, qty, "sell")
                    placed += 1 if res and res.get("status") not in ("rejected", "failed") else 0
                    print(f"    [ok] sold {s} qty={qty} -> {res}")
                except Exception as e:
                    print(f"    [err] sell {s}: {e}")
    return placed


def main() -> int:
    parser = argparse.ArgumentParser(description="Liquidate paper account to cash.")
    parser.add_argument("--confirm", action="store_true",
                        help="actually place liquidating sell orders (default: dry-run)")
    parser.add_argument("--yes", action="store_true", help="bypass interactive confirm")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    if not ALPACA_AVAILABLE:
        print("[err] alpaca-py not available; cannot liquidate.")
        return 1

    client = get_client_instance()
    if getattr(client, "is_mock", False):
        print("[warn] Client is in MOCK mode — nothing real will happen. Aborting.")
        return 1

    snap = snapshot(client)
    print("[snapshot] Account BEFORE liquidation:")
    print(json.dumps(snap, indent=2, default=str))

    if not args.confirm:
        print("\n[dry-run] No sells placed. Re-run with --confirm to liquidate "
              "(after snapshotting this output for the record).")
        liquidate(client, simulate=True)
        return 0

    if not args.yes:
        print("\nWARNING: This will SELL every open position at the market. This is irreversible.")
        resp = input("Type 'LIQUIDATE' to confirm: ")
        if resp.strip().upper() != "LIQUIDATE":
            print("[abort] Confirmation string not matched.")
            return 1

    placed = liquidate(client, simulate=False)
    print(f"[done] Placed {placed} liquidating order(s). "
          "Verify zero open positions before pointing agent-trade at this account.")
    return 0


if __name__ == "__main__":
    sys.exit(main())