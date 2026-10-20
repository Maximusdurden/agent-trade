import os
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv
load_dotenv(r'Z:\python\projects\agent-trade\.env')

api = tradeapi.REST(os.getenv('ALPACA_API_KEY'), os.getenv('ALPACA_SECRET_KEY'), os.getenv('APCA_API_BASE_URL', 'https://paper-api.alpaca.markets'), api_version='v2')
orders = api.list_orders(status='all', limit=50)
for o in orders:
    sub = str(o.submitted_at)
    if '2026-10-19' in sub:
        print(sub, o.symbol, o.side, o.qty, o.status, o.filled_avg_price, o.id)
