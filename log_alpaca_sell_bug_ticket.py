import sys
import os

# Ensure the library folder is in Python search path
sys.path.insert(0, r"Z:\python\projects")

try:
    from library.jira_logger import JiraLogger
except ImportError as e:
    print(f"[ERROR] Failed to import JiraLogger: {e}")
    sys.exit(1)

BUG_TICKETS = [
    {
        "summary": "AGE-303 Bug: Equity SELL orders fail on Alpaca due to active bracket TP/SL order locks",
        "description": "When executing a BUY order, the trading agent places an Alpaca bracket order containing Take-Profit (limit) and Stop-Loss (stop) legs. Once filled, these TP/SL legs are active on the Alpaca exchange. If the trading brain subsequently proposes a SELL order to liquidate or trim the position (due to a trend shift or emergency intraday shock), Alpaca rejects the sell order with 'insufficient qty available for order (requested: X, available: 0)'. This occurs because 100% of the position's shares are locked by the active TP/SL orders on the exchange. As a result, no sell orders are ever successfully executed or logged to the database, and consequently no sells are displayed in the Broker-Side Executed Orders widget.\n\nResolution Requirements:\n1. Implement a method in AlpacaClient to retrieve and cancel open/active limit/stop orders for a specific symbol on Alpaca prior to executing a new market order on that symbol.\n2. In alpaca_client.py (or runner.py), when executing a SELL action, check for and cancel any active orders (specifically the TP/SL bracket legs) for the target symbol first, freeing up the shares for liquidation.\n3. Integrate appropriate unit/mock tests to verify the cancel-and-sell logic works under both mock and live configurations.",
        "issuetype": "Bug"
    }
]

def create_jira_tickets():
    print("=" * 60)
    print("CREATING ALPACA SELL LOCK BUG TICKET IN JIRA")
    print("=" * 60)
    
    logger = JiraLogger()
    if not logger.user_email or not logger.api_token:
        print("[ERROR] JIRA credentials are missing. Please verify root .env configuration.")
        return
        
    for idx, issue in enumerate(BUG_TICKETS, 1):
        print(f"\n[{idx}/{len(BUG_TICKETS)}] Creating: '{issue['summary']}'...")
        payload = {
            "fields": {
                "project": {
                    "key": logger.project_key
                },
                "summary": issue["summary"],
                "description": issue["description"],
                "issuetype": {
                    "name": issue["issuetype"]
                }
            }
        }
        res = logger._make_request("/rest/api/2/issue", method="POST", data=payload)
        if res and "key" in res:
            print(f"  [SUCCESS] Created live JIRA ticket: {res['key']}")
        else:
            print(f"  [ERROR] Failed to create JIRA ticket.")
            
    print("\n" + "=" * 60)
    print("BUG TICKETS SUCCESSFULLY CREATED!")
    print("=" * 60)

if __name__ == "__main__":
    create_jira_tickets()
