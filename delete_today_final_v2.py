import os
import requests
from dotenv import load_dotenv
load_dotenv(r'Z:\python\projects\agent-trade\.env')

wp_url = os.getenv('WP_URL', 'https://treatmotivated.capital')
wp_user = os.getenv('WP_USER', 'admin')
wp_pass = os.getenv('WP_APP_PASSWORD')

resp = requests.get(f"{wp_url}/wp-json/wp/v2/posts?per_page=10", auth=(wp_user, wp_pass))
for p in resp.json():
    date_str = p.get('date', '')
    title = p.get('title', {}).get('rendered', '')
    pid = p.get('id')
    print(pid, date_str, title)
    if '2026-10-19' in date_str or '2026-10-19' in title:
        print("Deleting:", pid)
        requests.delete(f"{wp_url}/wp-json/wp/v2/posts/{pid}?force=true", auth=(wp_user, wp_pass))
