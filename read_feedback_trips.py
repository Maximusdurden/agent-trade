with open(r'Z:\python\projects\agent-trade\core\feedback.py', 'r') as f:
    text = f.read()
start = text.find('def compute_closed_round_trips')
print(text[start:start+1500])
