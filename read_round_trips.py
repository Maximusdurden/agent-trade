with open(r'Z:\python\projects\agent-trade\core\blog_stats.py', 'r') as f:
    text = f.read()
print(text[text.find('def round_trips_to_dexter_df'):text.find('def round_trips_to_dexter_df')+1000])
