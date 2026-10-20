import os
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv
load_dotenv(r'Z:\python\projects\agent-trade\.env')

key = os.getenv('APCA_API_KEY_ID')
secret = os.getenv('APCA_API_SECRET_KEY')
base_url = os.getenv('APCA_API_BASE_URL')

api = tradeapi.REST(key, secret, base_url, api_version='v2')
orders = api.list_orders(status='all', limit=50)
for o in orders:
    print(o.submitted_at, o.symbol, o.side, o.qty, o.status, o.filled_avg_price)
