import sys
import os

# Add parent directory of library (Z:\python\projects) to python path
sys.path.insert(0, r"Z:\python\projects")

try:
    from library.jira_logger import JiraLogger
except ImportError as e:
    print(f"[ERROR] Failed to import JiraLogger: {e}")
    sys.exit(1)

def close_punch_list_tickets():
    print("=" * 60)
    print("TRANSITIONING PUNCH LIST JIRA TICKETS TO DONE")
    print("=" * 60)
    
    logger = JiraLogger()
    if not logger.user_email or not logger.api_token:
        print("[ERROR] Jira credentials are missing. Check your shared .env file.")
        return
        
    # The set of punch list tickets we created and successfully implemented
    tickets_to_close = [
        "TMCL-578",  # UI/UX: Support responsive chart visualizers and beautiful no-code presentation in Q&A Analyst
        "TMCL-579",  # Prompt: Refactor Q&A Analyst system prompt to forbid code and generate QuickChart charts
        "TMCL-580",  # DB: Implement query and summarizer engine for historical successes and failures
        "TMCL-581",  # Strategy: Integrate historical trade successes and failures into Strategist and Execution Brain
    ]
    
    print(f"Analyzing and transitioning {len(tickets_to_close)} tickets under project {logger.project_key}...")
    
    closed_count = 0
    for key in tickets_to_close:
        print(f"\nChecking status and transitions for issue: {key}...")
        
        # 1. Fetch current issue details to check its status
        issue_res = logger._make_request(f"/rest/api/2/issue/{key}", method="GET")
        if not issue_res:
            print(f"  [WARNING] Could not retrieve details for issue {key}. It may not exist.")
            continue
            
        status = issue_res.get("fields", {}).get("status", {}).get("name", "Unknown")
        print(f"  Current Status: {status}")
        
        if status.lower() in ("done", "closed", "resolved", "completed"):
            print(f"  [SKIP] Issue {key} is already in a completed state.")
            closed_count += 1
            continue
            
        # 2. Retrieve valid transitions
        trans_res = logger._make_request(f"/rest/api/2/issue/{key}/transitions", method="GET")
        if not trans_res:
            print(f"  [ERROR] Could not fetch transitions for {key}.")
            continue
            
        transitions = trans_res.get("transitions", [])
        done_transition_id = None
        for t in transitions:
            name = t.get("to", {}).get("name", "").lower()
            if name in ("done", "completed", "closed", "resolved"):
                done_transition_id = t.get("id")
                print(f"  Found transition to 'Done' state: '{t.get('name')}' (ID: {done_transition_id})")
                break
                
        # 3. Transition the issue
        if done_transition_id:
            payload = {
                "transition": {
                    "id": done_transition_id
                }
            }
            res = logger._make_request(f"/rest/api/2/issue/{key}/transitions", method="POST", data=payload)
            print(f"  [SUCCESS] Transitioned ticket {key} to 'Done'!")
            closed_count += 1
        else:
            print(f"  [WARNING] No 'Done' transition path found for ticket {key}.")
            
    print("\n" + "=" * 60)
    print(f"[COMPLETE] TICKET CLOSING WORKFLOW COMPLETE! {closed_count}/{len(tickets_to_close)} tickets closed.")
    print("=" * 60)

if __name__ == "__main__":
    close_punch_list_tickets()
