with open(r'Z:\python\projects\agent-trade\tools\blog_update.py', 'r') as f:
    text = f.read()

# Let's see how _build_and_publish uses total_pnl or trades
start = text.find('def _build_and_publish')
if start == -1:
    start = text.find('def publish')
print(text[start:start+3000])
