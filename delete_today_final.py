import os
import requests
from dotenv import load_dotenv
load_dotenv(r'Z:\python\projects\agent-trade\.env')

wp_url = os.getenv('WP_URL', 'https://treatmotivated.capital')
wp_user = os.getenv('WP_USER', 'admin')
wp_pass = os.getenv('WP_APP_PASSWORD')

resp = requests.get(f"{wp_url}/wp-json/wp/v2/posts?per_page=10", auth=(wp_user, wp_pass))
for p in resp.json():
    print(p.get('id'), p.get('date'), p.get('title', {}).get('rendered'))
    if '2026-10-19' in p.get('date', '') or '2026-10-19' in p.get('title', {}].get('rendered', '')):
        print("Deleting:", p.get('id'))
        requests.delete(f"{wp_url}/wp-json/wp/v2/posts/{p.get('id')}?force=true", auth=(wp_user, wp_pass))
