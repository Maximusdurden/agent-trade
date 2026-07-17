import sys
import os

# Ensure the library folder is in Python search path
sys.path.insert(0, r"Z:\python\projects")

try:
    from library.jira_logger import JiraLogger
except ImportError as e:
    print(f"[ERROR] Failed to import JiraLogger: {e}")
    sys.exit(1)

PUNCH_LIST_ISSUES = [
    {
        "summary": "AGE-303 UI/UX: Support responsive chart visualizers and beautiful no-code presentation in Q&A Analyst",
        "description": "Implement a frontend update in dashboard.py to support rendering images (including QuickChart URLs) in the Strategy Q&A Analyst chat.\n\n"
                       "Requirements:\n"
                       "1. Enhance the client-side JavaScript botResponse parser in dashboard.py to support converting markdown image syntax `![alt](url)` into a responsive HTML `<img>` tag.\n"
                       "2. Style the images with a premium look (rounded corners, subtle box shadow, smooth hover-scale transition, maximum width of 100%).\n"
                       "3. Optimize chat log scrolling to auto-scroll to the bottom when charts finish loading.",
        "issuetype": "Story"
    },
    {
        "summary": "AGE-304 Prompt: Refactor Q&A Analyst system prompt to forbid code and generate QuickChart charts",
        "description": "Update the Q&A Analyst system prompt in dashboard.py to ensure absolute code-free, highly visual responses.\n\n"
                       "Requirements:\n"
                       "1. Add strict system instructions to never output Python, Javascript, SQL, or other code blocks.\n"
                       "2. Instruct the model to formulate all quantitative answers, trends, return comparisons, and hypotheses into beautiful embedded QuickChart.io visual charts using markdown/HTML images.\n"
                       "3. Include a rich template section for generating standard QuickChart URLs (bar, line, pie, radar) with customizable color palettes that match our dark/teal theme.",
        "issuetype": "Story"
    },
    {
        "summary": "AGE-305 DB: Implement query and summarizer engine for historical successes and failures",
        "description": "Develop a performance logging and summarizing module in database.py to calculate and return trade statistics.\n\n"
                       "Requirements:\n"
                       "1. Implement a function `get_performance_summary()` that analyzes the `trades` and `portfolio_history` tables.\n"
                       "2. Calculate win/loss statistics, average profit/loss per trade, largest winning trade, largest losing trade, current win rate, and equity drawdown metrics.\n"
                       "3. Return a clean, structured textual summary of successes and failures that can be easily fed into LLM prompts.",
        "issuetype": "Task"
    },
    {
        "summary": "AGE-306 Strategy: Integrate historical trade successes and failures into Strategist and Execution Brain",
        "description": "Integrate the database performance summary into MetaStrategist and TradingBrain prompts to enable agent-wide adaptive learning.\n\n"
                       "Requirements:\n"
                       "1. In strategist.py, fetch the performance summary and pass it to the MetaStrategist prompt, instructing the strategist to learn from recent failures and adjust rules accordingly.\n"
                       "2. In trading_brain.py, fetch the performance summary and pass it to the TradingBrain prompt, instructing it to avoid over-allocating on strategies or tickers with high recent failure rates.\n"
                       "3. In dashboard.py, pass the same performance summary into the Q&A Analyst prompt so it is aware of the exact performance history when queried.",
        "issuetype": "Story"
    }
]

def create_jira_tickets():
    print("=" * 60)
    print("CREATING PUNCH LIST TICKETS IN JIRA")
    print("=" * 60)
    
    logger = JiraLogger()
    if not logger.user_email or not logger.api_token:
        print("[ERROR] JIRA credentials are missing. Please verify root .env configuration.")
        return
        
    created_keys = []
    for idx, issue in enumerate(PUNCH_LIST_ISSUES, 1):
        print(f"\n[{idx}/{len(PUNCH_LIST_ISSUES)}] Creating: '{issue['summary']}'...")
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
            key = res["key"]
            print(f"  [SUCCESS] Created live JIRA ticket: {key}")
            created_keys.append(key)
        else:
            print(f"  [ERROR] Failed to create JIRA ticket.")
            
    print("\n" + "=" * 60)
    print("PUNCH LIST TICKETS SUCCESSFULLY CREATED!")
    print(f"Keys: {created_keys}")
    print("=" * 60)

if __name__ == "__main__":
    create_jira_tickets()
