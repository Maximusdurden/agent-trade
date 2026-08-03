import os
import urllib.request
import json

token = os.environ.get("CLOUDFLARE_API_TOKEN", "")

req = urllib.request.Request(
    "https://api.cloudflare.com/client/v4/zones",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
)
resp = urllib.request.urlopen(req)
data = json.load(resp)
for z in data.get("result", []):
    print(f"{z['name']}: {z['id']}")

# Look for agenttrade.us zone
for z in data.get("result", []):
    if "agenttrade" in z['name'] or "agenttrade" in z['name']:
        zone_id = z['id']
        print(f"\n--- DNS records for {z['name']} ---")
        req2 = urllib.request.Request(
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        resp2 = urllib.request.urlopen(req2)
        dns_data = json.load(resp2)
        for r in dns_data.get("result", []):
            print(f"  {r['type']} {r['name']} -> {r['content']}")