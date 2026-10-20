import os
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv
load_dotenv(r'Z:\python\projects\agent-trade\.env')

api = tradeapi.REST(os.getenv('ALPACA_API_KEY'), os.getenv('ALPACA_SECRET_KEY'), os.getenv('APCA_API_BASE_URL', 'https://paper-api.alpaca.markets'), api_version='v2')
orders = api.list_orders(status='all', limit=100)
today_orders = [o for o in orders if o.submitted_at.startswith('2026-10-19')]
print(f"Total orders for today: {len(today_orders)}")
for o in today_orders:
    print(o.submitted_at, o.symbol, o.side, o.qty, o.status, o.filled_avg_price)
