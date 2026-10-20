import os
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv
load_dotenv(r'Z:\python\projects\agent-trade\.env')

key = os.getenv('ALPACA_API_KEY')
secret = os.getenv('ALPACA_SECRET_KEY')
base_url = os.getenv('APCA_API_BASE_URL', 'https://paper-api.alpaca.markets')

print("Using key:", key[:5] + '...' if key else 'None')
api = tradeapi.REST(key, secret, base_url, api_version='v2')
orders = api.list_orders(status='all', limit=50)
print(f"Total orders fetched: {len(orders)}")
for o in orders:
    print(o.submitted_at, o.symbol, o.side, o.qty, o.status, o.filled_avg_price)
