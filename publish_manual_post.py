import os
import requests
from dotenv import load_dotenv
load_dotenv(r'Z:\python\projects\agent-trade\.env')

wp_url = os.getenv('WP_URL', 'https://treatmotivated.capital')
wp_user = os.getenv('WP_USER', 'admin')
wp_pass = os.getenv('WP_APP_PASSWORD')

title = "Market Update: 2026-10-19"
content = """Heckin quiet day in the markets today, freinds. No round-trips closed, so let's chat about our three strategies at a high level. Swing RSI-2 was staged to buy NVDA and SMH on mean-reversion dips, but no pullbacks materialized. Flat-base breakout sat idle — we need tight bases breaking out on volume, and nothing qualified. Options Model A also took the day off, as the pre-market range was too sleepy for a proper sweep. Total PnL was $0 on the day. Back at it tomorrow!"""

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
