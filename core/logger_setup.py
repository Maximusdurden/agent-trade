import logging
import sys
import os
from datetime import datetime

# Add the parent directory of library (Z:\python\projects) and agent-jira-client to python path
sys.path.insert(0, r"Z:\python\projects")
sys.path.insert(0, r"Z:\python\projects\agent-jira-client")

try:
    import agent_jira.jira_logger as jira_logger
    from agent_jira.jira_logger import setup_global_handler, setup_logger, log_exception
    JIRA_LOGGER_AVAILABLE = True
except ImportError:
    try:
        import library.jira_logger as jira_logger
        from library.jira_logger import setup_global_handler, setup_logger
        JIRA_LOGGER_AVAILABLE = True
    except ImportError:
        JIRA_LOGGER_AVAILABLE = False


class JiraLoggingHandler(logging.Handler):
    """
    Custom logging handler that intercepts WARNING, ERROR, and CRITICAL logs
    and automatically files or appends comments to corresponding Jira tickets.
    """
    def __init__(self, app_name="agent-trade", env="production", level=logging.ERROR):
        super().__init__(level=level)
        self.app_name = app_name
        self.env = env
        
    def emit(self, record):
        if not JIRA_LOGGER_AVAILABLE:
            return
            
        # Prevent infinite recursion if JiraLogger itself logs anything or if message contains [JiraLogger]
        msg = str(record.msg) if hasattr(record, 'msg') else ""
        if (record.name == "JiraLogger" or 
            "JiraLogger" in msg or 
            "[JiraLogger]" in msg or 
            "jira_logger" in record.pathname):
            return
            
        try:
            err_message = record.getMessage()
            err_type = f"Log-{record.levelname}"
            
            traceback_str = ""
            if record.exc_info:
                import traceback
                traceback_str = "".join(traceback.format_exception(*record.exc_info))
            else:
                traceback_str = f"Logged from {record.pathname}:{record.lineno} in {record.funcName}"
                
            metadata = {
                "Logger Name": record.name,
                "File Path": record.pathname,
                "Line": str(record.lineno),
                "Function": record.funcName,
                "Timestamp": datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S')
            }
            
            # Send to Jira (Fingerprint grouping is handled automatically by JiraLogger)
            # Reference the module-level singleton dynamically so we always use the
            # instance configured by setup_logger() (which reassigns jira_logger.default_logger),
            # rather than a stale copy captured at import time.
            jira_logger.default_logger.log_error(
                err_type=err_type,
                err_message=err_message,
                traceback_str=traceback_str,
                app_name=self.app_name,
                env=self.env,
                metadata=metadata
            )
        except Exception as e:
            # Fallback output to stderr to prevent crashing the main thread
            print(f"[JiraLoggingHandler] Failed to log to Jira: {e}", file=sys.stderr)

def setup_logging(app_name="agent-trade", env="production"):
    """
    Integrates the custom JIRA logging handler into the root logger
    and registers the global sys exception hook.
    """
    if not JIRA_LOGGER_AVAILABLE:
        print("[JiraLogger] Shared JIRA Logger library is not available in the python path.", file=sys.stderr)
        return
        
    # Dynamically setup logger with explicit configuration from config module
    from core import config
    try:
        if "setup_logger" in globals():
            setup_logger(
                site_url=config.JIRA_URL,
                project_key=config.JIRA_PROJECT_KEY,
                user_email=config.JIRA_EMAIL,
                api_token=config.JIRA_API_TOKEN,
                app_name=app_name,
                env=env
            )
    except Exception as e:
        print(f"[JiraLogger] setup_logger failed: {e}", file=sys.stderr)

    # Register global uncaught exception hook
    setup_global_handler(app_name=app_name, env=env)
    
    # Create JIRA handler
    jira_handler = JiraLoggingHandler(app_name=app_name, env=env, level=logging.ERROR)
    
    # Register handler on the root logger
    root_logger = logging.getLogger()
    
    # Avoid duplicate registrations if called multiple times
    has_jira_handler = False
    for handler in root_logger.handlers:
        if isinstance(handler, JiraLoggingHandler):
            has_jira_handler = True
            break
            
    if not has_jira_handler:
        root_logger.addHandler(jira_handler)
        print(f"[JiraLogger] Registered JiraLoggingHandler on root logger for '{app_name}' ({env}).")
