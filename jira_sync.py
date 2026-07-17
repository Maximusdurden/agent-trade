import os
import sys
import json
from pathlib import Path

# Add the parent directory of library (Z:\python\projects) to python path
sys.path.insert(0, r"Z:\python\projects")

try:
    from library.jira_logger import JiraLogger
except ImportError as e:
    print(f"[ERROR] Failed to import JiraLogger from library: {e}")
    sys.exit(1)

# The issue list to sync to Jira
ISSUES = [
    # Epic 1: Double-Protection AI Trading Scaffolding (All historical tasks)
    {"summary": "AGE-101 Scaffolding: Setup project directory, venv, and dependencies", "description": "Initialize Python 3.13 virtual environment and configure requirements.txt (alpaca-py, pandas, google-genai).", "issuetype": "Task", "is_done": True},
    {"summary": "AGE-102 Config: Secure env loading and trading universe setup", "description": "Build config.py to load Alpaca paper keys, Gemini model parameters, stock indices (SPY, QQQ) and crypto (SOL/USD).", "issuetype": "Task", "is_done": True},
    {"summary": "AGE-103 Database: Create SQLite schemas for logging decisions and trades", "description": "Develop database.py schemas for tables decisions, trades, portfolio_history, and strategy_history.", "issuetype": "Task", "is_done": True},
    {"summary": "AGE-104 Client: Build Alpaca Client with offline Mock fallbacks", "description": "Implement alpaca_client.py wrapping TradingClient and historical endpoints. Include fully simulated mock fallback.", "issuetype": "Feature", "is_done": True},
    {"summary": "AGE-105 indicators: Native Pandas technical indicators calculation engine", "description": "Implement data_provider.py to compute RSI 14, SMA 20/50, MACD, and Bollinger Bands using native Pandas.", "issuetype": "Feature", "is_done": True},
    {"summary": "AGE-106 Guardrails: Deterministic Risk Guardrail safety boundaries", "description": "Implement risk filters in guardrails.py enforcing 10% max allocation, 2% daily loss limit, and 5% cash buffer.", "issuetype": "Feature", "is_done": True},
    {"summary": "AGE-107 Strategist: Daily Meta-Strategist rule generator using Gemini", "description": "Create strategist.py executing daily reviews using Gemini 2.5 Flash to generate dynamic conditional rule paragraphs.", "issuetype": "Feature", "is_done": True},
    {"summary": "AGE-108 Brain: Tick-by-tick Execution Brain decisions parser", "description": "Build trading_brain.py to query strategist guidelines and current technical metrics, generating JSON trade actions.", "issuetype": "Feature", "is_done": True},
    {"summary": "AGE-109 Runner: Continuous integration loops with emergency shock checks", "description": "Build runner.py running 15-minute cycles with real-time shock triggers (>=3% stocks, >=8% crypto) for emergency strategy rewrites.", "issuetype": "Feature", "is_done": True},
    
    # Epic 2: Real-World Execution Sprints (New active tasks)
    {"summary": "AGE-201 Sprint 1: US Market Hours check in Guardrails", "description": "Add is_market_open_check() in guardrails.py to restrict SPY/QQQ trading to Mon-Fri 9:30 AM - 4:00 PM EST, while allowing SOLUSD 24/7.", "issuetype": "Story", "is_done": True},
    {"summary": "AGE-202 Sprint 1: Crypto fractional rounding sizes support", "description": "Enable float-precision (4 decimals) for cryptocurrency buy/sell operations in guardrails.py instead of integer casting.", "issuetype": "Story", "is_done": True},
    {"summary": "AGE-203 Sprint 1: Order execution fill polling in Alpaca Client", "description": "Refactor execute_market_order() to poll submitted order status for 5 seconds to secure filled_avg_price before logging.", "issuetype": "Story", "is_done": True},
    {"summary": "AGE-204 Sprint 2: Multi-Timeframe High-Precision 15m indicator feeds", "description": "Update get_historical_bars() and DataProvider to compute indicators on 15m bars while fetching separate daily candles for returns.", "issuetype": "Story", "is_done": True},
    {"summary": "AGE-205 Sprint 2: Implement broker-side exchange bracket orders", "description": "Add design/logic to support passing Stop-Loss and Take-Profit bounds directly in initial Alpaca order submissions.", "issuetype": "Task", "is_done": False},
    {"summary": "AGE-206 Sprint 3: Anti-Whipsaw minimum holding period guardrails", "description": "Query database to enforce a 4-hour hold limit between consecutive buy/sell actions of the same asset to eliminate churn.", "issuetype": "Story", "is_done": True},
]

def sync_to_jira():
    print("=" * 60)
    print("AUTOMATED JIRA SYNC UTILITY (AGE -> TMCL Project)")
    print("Using Shared JIRA Logger Library: library.jira_logger")
    print("=" * 60)
    
    # Instantiate JiraLogger - it automatically loads environment variables and sets defaults
    logger = JiraLogger()
    
    if not logger.user_email or not logger.api_token:
        print("\n[ERROR] MISSING JIRA CREDENTIALS!")
        print("Please configure your Jira credentials in your .env file:")
        print("  JIRA_USER_EMAIL=your_atlassian_email@domain.com")
        print("  JIRA_API_TOKEN=your_atlassian_api_token")
        print("\nTo generate an Atlassian API Token, visit:")
        print("--> https://id.atlassian.com/manage-profile/security/api-tokens")
        print("=" * 60)
        return
        
    print(f"Connecting to Jira: {logger.site_url}...")
    print(f"Syncing issues to Project '{logger.project_key}'...\n")
    
    success_count = 0
    
    for idx, issue in enumerate(ISSUES, 1):
        print(f"[{idx}/{len(ISSUES)}] Syncing: '{issue['summary']}'...")
        
        # Build standard Jira fields
        payload = {
            "fields": {
                "project": {
                    "key": logger.project_key
                },
                "summary": issue["summary"],
                "description": issue["description"],
                "issuetype": {
                    "name": issue["issuetype"] if issue["issuetype"] in ("Task", "Bug", "Story", "Epic") else "Task"
                }
            }
        }
        
        # Create Issue via JiraLogger wrapper
        res = logger._make_request("/rest/api/2/issue", method="POST", data=payload)
        
        if res and "key" in res:
            key = res["key"]
            status_text = "DONE" if issue["is_done"] else "TODO"
            print(f"  [SUCCESS] Created successfully! Key: {key} | Target Status: {status_text}")
            success_count += 1
            
            # If issue should be marked as "Done", try transitioning it
            if issue["is_done"]:
                # Query possible transitions for this issue
                trans_res = logger._make_request(f"/rest/api/2/issue/{key}/transitions", method="GET")
                if trans_res:
                    transitions = trans_res.get("transitions", [])
                    done_transition_id = None
                    for t in transitions:
                        name = t.get("to", {}).get("name", "").lower()
                        if name in ("done", "completed", "closed", "resolved"):
                            done_transition_id = t.get("id")
                            break
                    
                    if done_transition_id:
                        transition_payload = {
                            "transition": {
                                "id": done_transition_id
                            }
                        }
                        logger._make_request(f"/rest/api/2/issue/{key}/transitions", method="POST", data=transition_payload)
                        print(f"  [CLOSED] Transitioned key {key} to 'Done' successfully!")
                    else:
                        print(f"  [WARNING] 'Done' transition not found. Left as backlog task.")
                else:
                    print(f"  [WARNING] Could not retrieve transitions. Left as backlog task.")
        else:
            print(f"  [ERROR] Failed to create issue. Check Jira configuration or permissions.")
            
    print("\n" + "=" * 60)
    print(f"[COMPLETE] SYNC WORKFLOW COMPLETE! {success_count}/{len(ISSUES)} issues processed.")
    print("=" * 60)

if __name__ == "__main__":
    sync_to_jira()
