from core.alpaca_client import AlpacaClient
client = AlpacaClient()
orders = client.list_orders(status='all', limit=50)
print(f"Total orders fetched: {len(orders)}")
for o in orders:
    print(o.get('submitted_at'), o.get('symbol'), o.get('side'), o.get('qty'), o.get('status'), o.get('filled_avg_price'))
