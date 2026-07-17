import sys
import os

sys.path.insert(0, r"Z:\python\projects")

try:
    from library.jira_logger import JiraLogger
except ImportError as e:
    print(f"[ERROR] Failed to import JiraLogger: {e}")
    sys.exit(1)

NEXT_STEPS = [
    {
        "summary": "AGE-406 Feature: Implement Capital Rebalancing Cash Guardrail",
        "description": "Add a capital rebalancing guardrail in guardrails.py that restricts buy trades if the remaining cash balance falls below a specified threshold (e.g. 10% of total portfolio equity), ensuring a safe cash reserve cushion.",
        "issuetype": "Task"
    }
]

def create_next_steps():
    print("=" * 60)
    print("CREATING REMAINING REBALANCING GUARDRAIL JIRA TICKET")
    print("=" * 60)
    
    logger = JiraLogger()
    if not logger.user_email or not logger.api_token:
        print("[ERROR] JIRA credentials are missing. Please verify root .env configuration.")
        return
        
    created_keys = []
    for idx, issue in enumerate(NEXT_STEPS, 1):
        print(f"\n[{idx}/{len(NEXT_STEPS)}] Creating: '{issue['summary']}'...")
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
    print(f"REMAINING TICKET CREATED IN JIRA! Keys: {created_keys}")
    print("=" * 60)

if __name__ == "__main__":
    create_next_steps()
