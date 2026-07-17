import sys
import os

# Ensure the library folder is in Python search path
sys.path.insert(0, r"Z:\python\projects")

try:
    from library.jira_logger import JiraLogger
except ImportError as e:
    print(f"[ERROR] Failed to import JiraLogger: {e}")
    sys.exit(1)

KPI_TIMEZONE_ISSUES = [
    {
        "summary": "Feature: Dashboard Timezone Alignment to US/Eastern (America/New_York)",
        "description": "Align all displayed timestamps and dates in the AGE Desk dashboard to the US Eastern timezone (America/New_York).\n\n"
                       "Requirements:\n"
                       "1. Create JS utility functions `parseUtcTimestamp(ts)` and `formatToEastern(ts)` in dashboard.py to safely parse UTC database dates and convert them to America/New_York locale strings.\n"
                       "2. Update the AI Strategy Decision Stream cards to format dec.timestamp to US Eastern Time.\n"
                       "3. Update the Broker-Side Executed Orders table to format t.timestamp to US Eastern Time.\n"
                       "4. Update the Equity Valuation Curve chart labels (x-axis) to display time in US Eastern Time.\n"
                       "5. Update the Copilot Chat History Markdown export title and message timestamps to align with US Eastern Time.",
        "issuetype": "Story"
    },
    {
        "summary": "Feature: Expandable Equity Valuation Curve with Maximized KPI Analytics Workspace",
        "description": "Transform the Equity Valuation Curve panel into an interactive performance workstation with maximize capabilities.\n\n"
                       "Requirements:\n"
                       "1. Add a maximize/minimize toggle button to the Equity Valuation Curve panel header with Lucide 'maximize-2' / 'minimize-2' icons.\n"
                       "2. Implement CSS transitions and a fixed glassmorphic modal layout for the `#equity-curve-panel.maximized` class (matching the Strategy Q&A look and feel).\n"
                       "3. In maximized focus mode, display a dual-pane layout:\n"
                       "   - Left Pane (70% width): Main Interactive Chart canvas with selectable chart metric filters (Portfolio Equity, Unrealized PnL, Cash Drawdown) and granularity options.\n"
                       "   - Right Pane (30% width): A premium performance analytics panel containing beautifully styled glassmorphic KPI cards displaying calculated statistics:\n"
                       "     * Total Return % (current equity vs. initial equity)\n"
                       "     * Max Drawdown % (peak-to-trough historical drawdown)\n"
                       "     * Sharpe Ratio (estimated annualized)\n"
                       "     * Win Rate % (percentage of profitable executed orders)\n"
                       "     * Profit Factor (total gross profit divided by gross loss)\n"
                       "     * Total Executed Trades\n"
                       "4. Implement all calculation formulas inside client-side JS using the fetched telemetry status data, rendering real-time metrics instantly on maximizing.",
        "issuetype": "Story"
    }
]

def create_jira_tickets():
    print("=" * 60)
    print("CREATING TIMEZONE & KPI GRAPH INTERACTIVE WORKSPACE TICKETS IN JIRA")
    print("=" * 60)
    
    logger = JiraLogger()
    if not logger.user_email or not logger.api_token:
        print("[ERROR] JIRA credentials are missing. Please verify root .env configuration.")
        return
        
    for idx, issue in enumerate(KPI_TIMEZONE_ISSUES, 1):
        print(f"\n[{idx}/{len(KPI_TIMEZONE_ISSUES)}] Creating: '{issue['summary']}'...")
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
    print("TIMEZONE & KPI WORKSPACE TICKETS SUCCESSFULLY CREATED!")
    print("=" * 60)

if __name__ == "__main__":
    create_jira_tickets()
