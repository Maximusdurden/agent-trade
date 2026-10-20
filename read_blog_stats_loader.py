with open(r'Z:\python\projects\agent-trade\core\blog_stats.py', 'r') as f:
    text = f.read()
start = text.find('def load_realized_trades_df')
if start == -1:
    start = text.find('def round_trips_to_dexter_df')
print(text[start:start+1800])
