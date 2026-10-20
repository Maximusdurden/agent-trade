import os
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv
load_dotenv(r'Z:\python\projects\agent-trade\.env')

api = tradeapi.REST(os.getenv('ALPACA_API_KEY'), os.getenv('ALPACA_SECRET_KEY'), os.getenv('APCA_API_BASE_URL', 'https://paper-api.alpaca.markets'), api_version='v2')
activities = api.get_activities(activity_type='FILL')
for a in activities:
    if '2026-10-19' in str(a.transaction_time):
        print(a.transaction_time, a.symbol, a.side, a.qty, a.price)
