#!/usr/bin/env python3
"""Check live Alpaca account: positions, recent orders, account state."""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import OrderSide, QueryOrderStatus

client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)

print(f"Paper mode: {ALPACA_PAPER}")
print("=" * 70)

# Account
acct = client.get_account()
print(f"\nACCOUNT: equity={acct.equity} cash={acct.cash} buying_power={acct.buying_power}")
print(f"  portfolio_value={acct.portfolio_value} status={acct.status}")

# Positions
print("\nPOSITIONS:")
positions = client.get_all_positions()
if not positions:
    print("  (none)")
for p in positions:
    print(f"  {p.symbol}: qty={p.qty} avg_entry={p.avg_entry_price} market={p.current_price} "
          f"unrealized={p.unrealized_pl} pct={p.unrealized_plpc}")

# Recent orders (last 7 days)
print("\nORDERS (last 14 days):")
req = GetOrdersRequest(
    status=QueryOrderStatus.ALL,
    after=datetime.now() - timedelta(days=14),
    limit=200,
)
orders = client.get_orders(filter=req)
if not orders:
    print("  (none)")
for o in orders:
    print(f"  {o.submitted_at} | {o.symbol} | {o.side} | qty={o.qty} filled={o.filled_qty} "
          f"type={o.type} status={o.status} filled_avg={o.filled_avg_price}")

print("\nPORTFOLIO HISTORY (last 14 days, 1D):")
try:
    hist = client.get_portfolio_history(
        start=(datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d"),
        end=datetime.now().strftime("%Y-%m-%d"),
        timeframe="1D",
    )
    if hist and hist.equity:
        for i, eq in enumerate(hist.equity):
            ts = hist.timestamp[i]
            print(f"  {datetime.fromtimestamp(ts).strftime('%Y-%m-%d')}: equity={eq:.2f}")
except Exception as e:
    print(f"  ERR: {e}")