import alpaca_trade_api as tradeapi
from core.config import APIS
api = tradeapi.REST(APIS['alpaca']['key'], APIS['alpaca']['secret'], APIS['alpaca']['base_url'], api_version='v2')
orders = api.list_orders(status='all', limit=50)
for o in orders:
    print(o.submitted_at, o.symbol, o.side, o.qty, o.status, o.filled_avg_price)
