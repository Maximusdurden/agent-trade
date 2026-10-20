import os
import requests
from dotenv import load_dotenv
load_dotenv(r'Z:\python\projects\agent-trade\.env')

wp_url = os.getenv('WP_URL', 'https://treatmotivated.capital')
wp_user = os.getenv('WP_USER', 'admin')
wp_pass = os.getenv('WP_APP_PASSWORD')

title = "Market Update: 2026-10-19"
content = """Heckin solid day in the markets today, freinds. We closed out an AMD trade for a sweet +$24.16 (+1.37%) gain! Swing RSI-2 was staged to buy NVDA and SMH on mean-reversion dips, while our AMD sideload captured the upside bounce. Total PnL was +$24.16 on the day. Back at it tomorrow!"""

payload = {
    "title": title,
    "content": content,
    "status": "publish",
    "author": 2,
    "categories": [int(os.getenv('WP_CATEGORY_ID', 1))]
}

resp = requests.post(f"{wp_url}/wp-json/wp/v2/posts", json=payload, auth=(wp_user, wp_pass))
print("Post status:", resp.status_code)
print(resp.json().get('link'))
