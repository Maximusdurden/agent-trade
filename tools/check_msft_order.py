#!/usr/bin/env python3
"""Check MSFT order fill timing and recent blog posts."""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)

print("MSFT order details:")
req = GetOrdersRequest(
    status=QueryOrderStatus.ALL,
    symbols=["MSFT"],
    after=datetime.now() - timedelta(days=14),
    limit=10,
)
orders = client.get_orders(filter=req)
for o in orders:
    print(f"  submitted={o.submitted_at}")
    print(f"  updated={o.updated_at}")
    print(f"  filled_at={o.filled_at}")
    print(f"  status={o.status} filled_qty={o.filled_qty} filled_avg={o.filled_avg_price}")
    print(f"  created_at={o.created_at}")
    print(f"  expired_at={o.expired_at}")
    print(f"  legs={o.legs}")

# Also check all orders 9/28-9/30 to see if there was any sell
print("\nAll orders 9/28 - 9/30:")
req2 = GetOrdersRequest(
    status=QueryOrderStatus.ALL,
    after=datetime(2026, 9, 28),
    limit=50,
)
orders2 = client.get_orders(filter=req2)
for o in orders2:
    print(f"  {o.submitted_at} | {o.symbol} | {o.side} | qty={o.qty} filled={o.filled_qty} "
          f"status={o.status} filled_avg={o.filled_avg_price}")