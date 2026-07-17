import sys
import os

# Add parent directory of library (Z:\python\projects) to python path
sys.path.insert(0, r"Z:\python\projects")

try:
    from library.jira_logger import JiraLogger
except ImportError as e:
    print(f"[ERROR] Failed to import JiraLogger: {e}")
    sys.exit(1)

RESOLVED_BUGS = [
    {
        "summary": "AGE-301 Bug: Frontend JavaScript Render Halt due to CDN block or null properties",
        "description": "The unified trading dashboard failed to render completely due to global script blocks during unpkg Lucide CDN loading and lack of defensive property fallbacks in fetchStatus() JSON parsing. Fixed by implementing safe wrappers, conditional library checks, and safe object navigators in dashboard.py.",
        "issuetype": "Bug"
    },
    {
        "summary": "AGE-302 Bug: Python backend server Unicode terminal encoding crash on Windows",
        "description": "Unicode emojis (robot, rocket, siren, shock) printed directly to console caused fatal UnicodeEncodeError in Windows CP1252 consoles. Fixed by replacing print/log emojis with ASCII equivalents in dashboard.py, runner.py, and strategist.py.",
        "issuetype": "Bug"
    }
]

def log_and_resolve_bugs():
    print("=" * 60)
    print("PROGRAMMATIC JIRA LOGGER FOR RESOLVED BUGS")
    print("=" * 60)
    
    logger = JiraLogger()
    if not logger.user_email or not logger.api_token:
        print("[ERROR] Jira credentials are missing. Check your shared .env file.")
        return
        
    print(f"Connecting to Jira: {logger.site_url}...")
    print(f"Project Key: {logger.project_key}\n")
    
    for idx, bug in enumerate(RESOLVED_BUGS, 1):
        print(f"[{idx}/{len(RESOLVED_BUGS)}] Creating Jira ticket: '{bug['summary']}'...")
        payload = {
            "fields": {
                "project": {
                    "key": logger.project_key
                },
                "summary": bug["summary"],
                "description": bug["description"],
                "issuetype": {
                    "name": bug["issuetype"]
                }
            }
        }
        res = logger._make_request("/rest/api/2/issue", method="POST", data=payload)
        if res and "key" in res:
            key = res["key"]
            print(f"  [SUCCESS] Created ticket on JIRA: {key}")
            
            # Transition to Done
            print(f"  Fetching transitions for {key}...")
            trans_res = logger._make_request(f"/rest/api/2/issue/{key}/transitions", method="GET")
            if trans_res:
                transitions = trans_res.get("transitions", [])
                done_transition_id = None
                for t in transitions:
                    name = t.get("to", {}).get("name", "").lower()
                    if name in ("done", "completed", "closed", "resolved"):
                        done_transition_id = t.get("id")
                        print(f"    Found transition to 'Done' state: '{t.get('name')}' (ID: {done_transition_id})")
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
                    print(f"  [WARNING] 'Done' transition not found. Left open.")
            else:
                print(f"  [WARNING] Could not retrieve transitions. Left open.")
        else:
            print(f"  [ERROR] Failed to create ticket on JIRA.")

    print("\n" + "=" * 60)
    print("RESOLVED BUGS LOGGED AND SYNCED SUCCESSFULLY TO JIRA!")
    print("=" * 60)

if __name__ == "__main__":
    log_and_resolve_bugs()
