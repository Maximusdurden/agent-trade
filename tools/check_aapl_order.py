#!/usr/bin/env python3
"""Check AAPL order status from yesterday's swing EOD scan."""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)

print("AAPL orders (last 3 days):")
req = GetOrdersRequest(
    status=QueryOrderStatus.ALL,
    symbols=["AAPL"],
    after=datetime.now() - timedelta(days=3),
    limit=20,
)
orders = client.get_orders(filter=req)
if not orders:
    print("  (none)")
for o in orders:
    print(f"  submitted={o.submitted_at}")
    print(f"  updated={o.updated_at}")
    print(f"  filled_at={o.filled_at}")
    print(f"  status={o.status} filled_qty={o.filled_qty} filled_avg={o.filled_avg_price}")
    print(f"  type={o.type} tif={o.time_in_force}")
    print(f"  ---")

print("\nAll orders since 10/1 20:00 UTC:")
req2 = GetOrdersRequest(
    status=QueryOrderStatus.ALL,
    after=datetime(2026, 10, 1, 20, 0),
    limit=50,
)
orders2 = client.get_orders(filter=req2)
if not orders2:
    print("  (none)")
for o in orders2:
    print(f"  {o.submitted_at} | {o.symbol} | {o.side} | qty={o.qty} filled={o.filled_qty} "
          f"status={o.status} filled_avg={o.filled_avg_price}")