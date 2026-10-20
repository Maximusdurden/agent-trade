with open(r'Z:\python\projects\agent-trade\tools\blog_update.py', 'r') as f:
    text = f.read()
print(text[text.find('def run'):text.find('def run')+2500])
