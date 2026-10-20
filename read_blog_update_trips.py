with open(r'Z:\python\projects\agent-trade\tools\blog_update.py', 'r') as f:
    text = f.read()
start = text.find('def run')
print(text[start:start+1200])
