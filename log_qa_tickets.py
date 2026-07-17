import sys
import os

# Ensure the library folder is in Python search path
sys.path.insert(0, r"Z:\python\projects")

try:
    from library.jira_logger import JiraLogger
except ImportError as e:
    print(f"[ERROR] Failed to import JiraLogger: {e}")
    sys.exit(1)

QA_ISSUES = [
    {
        "summary": "AGE-301 Feature: Premium Maximize/Minimize Focus Mode for Strategy Q&A Analyst Widget",
        "description": "Implement a maximize/minimize focus toggle in the 'Strategy Q&A Analyst' widget header in dashboard.py.\n\n"
                       "Requirements:\n"
                       "1. Add a maximize button in the panel header next to the title using Lucide's 'maximize-2' / 'minimize-2' icons.\n"
                       "2. Implement CSS styling for .card-panel.maximized to turn the widget into a full-screen glassmorphic modal overlay (90vw, 90vh, fixed placement, z-index 9999).\n"
                       "3. Ensure the chat-log scroll height dynamically expands to calc(100% - 140px) inside the focus mode.\n"
                       "4. Implement smooth CSS transitions and scale-in animations for a premium desktop experience.\n"
                       "5. Allow dismissing the focus mode using the ESC key on the keyboard.",
        "issuetype": "Story"
    },
    {
        "summary": "AGE-302 Feature: Interactive Markdown Export & Clipboard Copy for Q&A History",
        "description": "Implement client-side export and clipboard utilities inside the Strategy Q&A Analyst widget in dashboard.py.\n\n"
                       "Requirements:\n"
                       "1. Add a small 'Copy' button (icon: copy) and 'Export' button (icon: download) to the widget's utility header.\n"
                       "2. Implement JavaScript click handlers:\n"
                       "   - Copy: Loop through the DOM elements of #chat-log, compile the sequence into a beautifully formatted Markdown string (e.g., '### Quant Investor:\\n...\\n\\n### AGE Copilot:\\n...'), and write to navigator.clipboard.\n"
                       "   - Export: Format the conversation to Markdown, create a text Blob, and trigger a browser download of a file named 'AGE_Copilot_Conversation_YYYYMMDD.md'.\n"
                       "3. Ensure a clean, subtle tooltip or temporary toast confirmation ('Copied to Clipboard!') appears near the clicked button to maintain high-quality feedback.",
        "issuetype": "Story"
    }
]

def create_jira_tickets():
    print("=" * 60)
    print("CREATING STRATEGY Q&A ANALYST TICKETS IN JIRA")
    print("=" * 60)
    
    logger = JiraLogger()
    if not logger.user_email or not logger.api_token:
        print("[ERROR] JIRA credentials are missing. Please verify root .env configuration.")
        return
        
    for idx, issue in enumerate(QA_ISSUES, 1):
        print(f"\n[{idx}/{len(QA_ISSUES)}] Creating: '{issue['summary']}'...")
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
    print("STRATEGY Q&A TICKETS SUCCESSFULLY CREATED!")
    print("=" * 60)

if __name__ == "__main__":
    create_jira_tickets()
