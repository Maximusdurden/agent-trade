import urllib.request
import json

urls = {
    "dashboard.agenttrade.us": "https://dashboard.agenttrade.us/api/status",
    "agenttrade-dashboard (direct)": "https://agenttrade-dashboard-loenftvakq-ue.a.run.app/api/status",
}

for label, url in urls.items():
    try:
        resp = urllib.request.urlopen(url, timeout=15)
        d = json.load(resp)
        print(f"\n{'='*60}")
        print(f"=== {label} ===")
        print(f"{'='*60}")
        acc = d.get("account", {})
        print(f"Account Equity: {acc.get('equity')}")
        print(f"Account Cash: {acc.get('cash')}")
        print(f"Account PnL: {acc.get('unrealized_pnl')}")
        print(f"Positions count: {len(d.get('positions', []))}")
        print(f"Decisions count: {len(d.get('decisions', []))}")
        print(f"Portfolio History points: {len(d.get('portfolio_history', []))}")
        print(f"Watchlist: {d.get('watchlist', [])[:5]}")
        print(f"Runner Status: {d.get('last_heartbeat', {}).get('status')}")
        print(f"Runner completed_at: {d.get('last_heartbeat', {}).get('completed_at')}")

        print(f"\nLatest 3 decisions:")
        for dec in d.get("decisions", [])[:3]:
            ts = dec.get("timestamp", "")
            action = dec.get("action", "")
            ticker = dec.get("ticker", "")
            reason = dec.get("reasoning", "")[:100]
            print(f"  {ts} | {action} | {ticker} | {reason}")

        print(f"\nPositions:")
        for pos in d.get("positions", []):
            print(f"  {pos.get('symbol')}: qty={pos.get('qty')} mkt_val={pos.get('market_value')} pnl={pos.get('unrealized_pnl')}")

        print(f"\nPortfolio History (first 3):")
        for h in d.get("portfolio_history", [])[:3]:
            print(f"  {h.get('timestamp')}: equity={h.get('equity')}")

        print(f"\nPortfolio History (last 3):")
        for h in d.get("portfolio_history", [])[-3:]:
            print(f"  {h.get('timestamp')}: equity={h.get('equity')}")
    except Exception as e:
        print(f"\n=== {label} ===")
        print(f"ERROR: {e}")