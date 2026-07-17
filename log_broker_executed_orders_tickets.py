import sys
import os

# Ensure the library folder is in Python search path
sys.path.insert(0, r"Z:\python\projects")

try:
    from library.jira_logger import JiraLogger
except ImportError as e:
    print(f"[ERROR] Failed to import JiraLogger: {e}")
    sys.exit(1)

ORDER_WORKSTATION_ISSUES = [
    {
        "summary": "Feature: Expandable Broker-Side Executed Orders panel with Maximize/Minimize focus workspace",
        "description": "Transform the static Broker-Side Executed Orders panel into an expandable focus workspace.\n\n"
                       "Requirements:\n"
                       "1. Add an id of `executed-orders-panel` to the Broker-Side Executed Orders card-panel.\n"
                       "2. Add a maximize/minimize toggle button to its panel-header with Lucide 'maximize-2' / 'minimize-2' icons.\n"
                       "3. Implement CSS transitions and a glassmorphic fullscreen layout for the `#executed-orders-panel.maximized` state (consistent with Strategy Q&A and Equity Valuation Curve).\n"
                       "4. In maximized focus mode, expand the table to display more rows without scrolling (increasing heights dynamically).\n"
                       "5. Bind window keyboard listener for 'Escape' key to exit focus mode safely.",
        "issuetype": "Story"
    },
    {
        "summary": "Feature: Ticker Filtering, Transactional Stats, and CSV Export for Executed Orders Workspace",
        "description": "Integrate interactive ticker filtering and quantitative summary details into the Executed Orders panel.\n\n"
                       "Requirements:\n"
                       "1. Add an interactive select element or tab row `#ticker-filter` in the panel header containing values for all stock indices and crypto (All Tickers, SPY, QQQ, SOL/USD).\n"
                       "2. Write JavaScript logic `filterExecutedOrders()` to filter rows on-screen instantly when a ticker is selected.\n"
                       "3. Render a secondary sub-header banner when maximized displaying dynamic, filtered transactional statistics:\n"
                       "   - Total Orders (Count of matching fills)\n"
                       "   - Total Buy Volume ($ value of filled buy orders)\n"
                       "   - Total Sell Volume ($ value of filled sell orders)\n"
                       "   - Net Portfolio Inflow/Outflow ($)\n"
                       "4. Integrate a premium 'Export CSV' button using Lucide 'download' to export the current filtered transaction logs into a standard spreadsheet file.",
        "issuetype": "Story"
    }
]

def create_jira_tickets():
    print("=" * 60)
    print("CREATING EXECUTED ORDERS WORKSPACE TICKETS IN JIRA")
    print("=" * 60)
    
    logger = JiraLogger()
    if not logger.user_email or not logger.api_token:
        print("[ERROR] JIRA credentials are missing. Please verify root .env configuration.")
        return
        
    for idx, issue in enumerate(ORDER_WORKSTATION_ISSUES, 1):
        print(f"\n[{idx}/{len(ORDER_WORKSTATION_ISSUES)}] Creating: '{issue['summary']}'...")
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
    print("EXECUTED ORDERS WORKSPACE TICKETS SUCCESSFULLY CREATED!")
    print("=" * 60)

if __name__ == "__main__":
    create_jira_tickets()
