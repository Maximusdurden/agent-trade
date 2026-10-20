with open(r'Z:\python\projects\agent-trade\tools\blog_update.py', 'r') as f:
    text = f.read()
start = text.find('if not df.empty:')
print(text[start:start+1500])
