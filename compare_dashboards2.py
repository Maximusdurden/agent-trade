import urllib.request
import json
import time

urls = [
    ("dashboard.agenttrade.us", "https://dashboard.agenttrade.us/api/status"),
    ("agenttrade-dashboard (direct)", "https://agenttrade-dashboard-loenftvakq-ue.a.run.app/api/status"),
]

results = {}
for label, url in urls:
    try:
        resp = urllib.request.urlopen(url, timeout=15)
        results[label] = json.load(resp)
        time.sleep(0.5)
    except Exception as e:
        print(f"{label}: ERROR - {e}")

for label, d in results.items():
    acc = d.get("account", {})
    print(f"\n--- {label} ---")
    print(f"  Equity: {acc.get('equity')}")
    print(f"  Cash: {acc.get('cash')}")
    print(f"  PnL: {acc.get('unrealized_pnl')}")
    print(f"  Positions: {len(d.get('positions', []))} entries")
    pos = d.get("positions", {})
    if isinstance(pos, dict):
        for sym, p in pos.items():
            print(f"    {sym}: {p.get('qty')} shares, mkt_val={p.get('market_value')}, pnl={p.get('unrealized_pnl')}")
    else:
        for p in pos:
            print(f"    {p.get('symbol')}: {p.get('qty')} shares")
    # Check API timestamp
    print(f"  First decision: {d.get('decisions', [{}])[0].get('timestamp','none')}")

# Check if they are literally different services
print("\n\n=== Checking if these resolve to different IPs ===")
import socket
for label, url in urls:
    host = url.split("://")[1].split("/")[0]
    ip = socket.gethostbyname(host)
    print(f"  {label} ({host}) -> {ip}")