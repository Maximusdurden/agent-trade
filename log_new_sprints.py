import sys
import os

sys.path.insert(0, r"Z:\python\projects")

try:
    from library.jira_logger import JiraLogger
except ImportError as e:
    print(f"[ERROR] Failed to import JiraLogger: {e}")
    sys.exit(1)

NEW_ISSUES = [
    {"summary": "AGE-207 Sprint 4: Programmatic Fibonacci Retracements calculation", "description": "Calculate Fibonacci retracement levels (23.6%, 38.2%, 50.0%, 61.8%) in data_provider.py over a 30-day lookback window and inject them as anchors in the prompt.", "issuetype": "Story", "is_done": False},
    {"summary": "AGE-208 Sprint 4: Psychological levels and Supply/Demand pivot zones", "description": "Compute key round-number psychological benchmarks and support/resistance pivot zones based on local swings in data_provider.py and pass them to the prompt.", "issuetype": "Story", "is_done": False},
    {"summary": "AGE-209 Sprint 5: Macro News integration for daily strategist updates", "description": "Fetch real-time news headlines via Alpaca News API or financial RSS feeds and feed them to the daily Meta-Strategist rule generator.", "issuetype": "Story", "is_done": False},
    {"summary": "AGE-210 Sprint 6: Volatility-Adjusted Quant Trade Sizing (ATR)", "description": "Implement Average True Range (ATR) or Bollinger Band width metrics to dynamically scale trade sizes (larger size in low volatility, smaller size in high volatility).", "issuetype": "Story", "is_done": False},
]

def log_new_sprints():
    print("=" * 60)
    print("LOGGING NEW ADVANCED SPRINTS TO JIRA (Sprints 4, 5, 6)")
    print("=" * 60)
    
    logger = JiraLogger()
    if not logger.user_email or not logger.api_token:
        print("[ERROR] Jira credentials are missing. Check your shared .env file.")
        return
        
    for idx, issue in enumerate(NEW_ISSUES, 1):
        print(f"\n[{idx}/{len(NEW_ISSUES)}] Creating ticket: '{issue['summary']}'...")
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
            print(f"  [ERROR] Failed to create ticket on JIRA.")
            
    print("\n" + "=" * 60)
    print("NEW SPRINTS LOGGED SUCCESSFULLY TO JIRA!")
    print("=" * 60)

if __name__ == "__main__":
    log_new_sprints()
