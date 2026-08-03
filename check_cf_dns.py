import os
import urllib.request
import json

token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
zone_id = "5e158eef264277d09828a6893ce47981"

req = urllib.request.Request(
    f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
)
resp = urllib.request.urlopen(req)
data = json.load(resp)

for r in data.get("result", []):
    name = r["name"]
    type_ = r["type"]
    content = r["content"]
    print(f"{type_:5} {name:45} -> {content}")