# filename: transition_task.py
import os
import sys

# Ensure stdout uses UTF-8
try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

sys.path.insert(0, r"Z:\python\projects\agent-jira-client")

from agent_jira import IssueManager

def run():
    manager = IssueManager()
    ticket = "TMCL-684"
    
    print(f"Transitioning {ticket} to 'In Progress'...")
    try:
        manager.transition_issue(ticket, "In Progress")
    except Exception as e:
        print(f"Transition to In Progress failed: {e}")
        
    comment = (
        "h3. 🚀 Cloud SRE Developer Agent: Phase 1 & 2 Execution Summary\n\n"
        "I have successfully executed the following migration and deployment tasks:\n\n"
        "* *Secret Synchronization*: Extracted Alpaca paper keys from dexter-trader/.env and Jira API credentials from projects/.env, writing them securely to agent-trade/.env.\n"
        "* *Alpaca Historical Data Patch*: Updated core/alpaca_client.py to import DataFeed safely and explicitly set feed=DataFeed.IEX on all StockBarsRequest instances.\n"
        "* *Local Task Retirement*: Successfully deleted the local Windows Scheduled Task AgentTradeRunner.\n"
        "* *Cloud Deployment*: Run deploy_cloud.ps1 to trigger Google Cloud Build, publish the container, and deploy the agent-trade-job Cloud Run Job and agent-trade-scheduler Cloud Scheduler trigger.\n\n"
        "All developer steps are complete, ready for QA validation by the Reviewer Agent."
    )
    
    print(f"Adding comment to {ticket}...")
    manager.add_comment(ticket, comment)
    
    print(f"Transitioning {ticket} to 'Done'...")
    try:
        manager.transition_issue(ticket, "Done")
        print(f"Successfully marked {ticket} as Completed/Done.")
    except Exception as e:
        print(f"Transition to Done failed: {e}")

if __name__ == "__main__":
    run()
