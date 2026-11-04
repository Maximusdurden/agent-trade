"""The job registry: every job, and the rule for deciding it worked.

WHY A CONFIG FILE AND NOT EXIT CODES
------------------------------------
Exit code 0 does not mean success. On 2026-11-03 `NBA Prop Daily` exited 0
while recording `markets: 0` -- it ran, it succeeded, and it did nothing,
because Kalshi had not listed the slate yet. A report built on exit codes
would have shown that as a healthy green row.

So each job carries a `success_rule`: a small expression evaluated against
the run's detail. The rule is the difference between "the process exited" and
"the job did its job".

RULES ARE DECLARATIVE, NOT CODE
-------------------------------
A rule is a dict of field -> predicate, evaluated against the detail dict the
collector extracts. Keeping it data means the registry can be edited without
touching the collector, and a bad rule fails loudly at load time rather than
silently marking everything ok.

    {"markets": (">", 0)}          markets must be greater than 0
    {"verdict": ("==", "OK")}      verdict must equal OK
    {"trades": (">=", 0)}          any value, including 0, is fine

A job with no rule falls back to exit-code-only, and is flagged as such in
the UI so the weaker guarantee is visible.

SOURCES
-------
    local      Windows Task Scheduler
    scheduler  Cloud Scheduler (which triggers a Cloud Run job)
    runjob     Cloud Run job execution history

A Cloud Scheduler job and the Cloud Run job it triggers are the SAME logical
job. They are linked by `target`, so the report shows one row per job rather
than two rows that disagree.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Outcome vocabulary. These five values are the whole point of the project.
# ---------------------------------------------------------------------------
OK = "ok"
NO_OP = "no_op"
FAILED = "failed"
MISSED = "missed"
SKIPPED = "skipped"
RUNNING = "running"
UNKNOWN = "unknown"

OUTCOMES = (OK, NO_OP, FAILED, MISSED, SKIPPED, RUNNING, UNKNOWN)

# Outcomes that mean "this needs attention". Used by the page's default view.
ATTENTION = (NO_OP, FAILED, MISSED)


@dataclass(frozen=True)
class Job:
    """One scheduled job, from any source."""
    job_id: str                  # stable, unique across sources
    name: str                   # human label
    source: str                 # 'local' | 'scheduler' | 'runjob'
    schedule: str               # cron or human description
    timezone: str               # IANA name, so the page can normalise
    enabled: bool
    category: str               # 'trading' | 'blog' | 'infra' | 'research'
    # The Cloud Run job this scheduler triggers, if any. Lets the collector
    # join a scheduler row to its execution history.
    target: str | None = None
    # Predicate on the run detail. Empty means exit-code-only.
    success_rule: dict[str, tuple[str, Any]] = field(default_factory=dict)
    # Where the log lives, for the "open log" link.
    log_hint: str | None = None
    # The EXACT Windows task name, which often differs from the display name.
    # `AgentTradeAMDMonitor` is the task; "AgentTrade AMD Monitor" is what a
    # human calls it. Using the display name silently matched nothing and
    # every local job reported "task not found".
    task_name: str | None = None
    # Logon-triggered tasks are not scheduled, so their last result is not a
    # statement about the schedule. Excluded from the attention list.
    scheduled: bool = True
    notes: str = ""


# ---------------------------------------------------------------------------
# LOCAL -- Windows Task Scheduler
# ---------------------------------------------------------------------------
# Schedules are transcribed from the live task definitions on 2026-11-04.
# They are descriptive only; the collector reads the real trigger from the
# scheduler so a changed schedule shows up without editing this file.
LOCAL_JOBS: tuple[Job, ...] = (
    Job("local:nba-prop-daily", "NBA Prop Daily", "local",
        "daily 10:00", "America/New_York", True, "trading",
        success_rule={"markets": (">", 0)},
        log_hint=r"Z:\python\projects\prediction-markets-research\out\runlog.jsonl",
        task_name="NBA Prop Daily",
        notes="Exits 0 even when Kalshi has not listed the slate yet."),

    Job("local:agenttrade-amd-monitor", "AgentTrade AMD Monitor", "local",
        "daily 20:00", "America/New_York", True, "trading",
        # No success rule: the monitor prints its verdict to stdout and sends
        # it to Discord; it writes no log file, so there is nothing to scrape.
        # Exit-code-only is the honest guarantee here, and the UI flags it.
        log_hint=r"Z:\python\projects\agent-trade\logs",
        task_name="AgentTradeAMDMonitor",
        notes="Was failing nightly on a cmd /c quoting bug until 2026-11-04. "
              "Verdict goes to Discord only, so no rule can be applied."),

    Job("local:agenttrade-sol-monitor", "AgentTrade SOL Monitor", "local",
        "daily 20:05", "America/New_York", True, "trading",
        log_hint=r"Z:\python\projects\agent-trade\logs",
        task_name="AgentTradeSOLMonitor",
        notes="Same quoting bug as AMD; fixed 2026-11-04. "
              "Verdict goes to Discord only, so no rule can be applied."),

    Job("local:modela-quote-sniffer", "ModelA Quote Sniffer", "local",
        "daily 09:30", "America/New_York", True, "trading",
        log_hint=r"Z:\python\projects\model-a-options\data_cache\quote_sniffer.log",
        task_name="ModelAQuoteSniffer"),

    Job("local:modela-eod-db-sync", "ModelA EOD DB Sync", "local",
        "daily 16:50", "America/New_York", True, "trading",
        log_hint=r"Z:\python\projects\model-a-options\logs\eod_db_sync.log",
        task_name="ModelAOptions EOD DB Sync"),

    Job("local:gcloud-usage-reporter", "GCloud AI Usage Reporter", "local",
        "daily 14:19", "America/New_York", True, "infra",
        log_hint=r"Z:\python\projects\gcloud-ai-tracker",
        task_name="GCloud AI Usage Reporter"),

    Job("local:webmaster-agent", "Webmaster Agent", "local",
        "daily 05:00", "America/New_York", True, "blog",
        log_hint=r"Z:\python\projects\dexter-trader\logs",
        task_name="WebmasterAgent"),

    Job("local:master-mcp-gateway", "Master MCP Gateway", "local",
        "on logon", "America/New_York", True, "infra",
        task_name="MasterMCPGateway",
        scheduled=False,
        # A logon-triggered task is not a scheduled job. Its last result is
        # whatever happened when the user last logged in, which may be days
        # ago and is not a failure of the schedule. Marking it `failed` put a
        # permanent false alarm at the top of the page.
        notes="Logon trigger, not scheduled. Last result reflects the last "
              "logon, so it is excluded from the attention list."),

    # --- disabled ---
    Job("local:agent-media-pipeline", "Agent Media Pipeline", "local",
        "daily 09:00", "America/New_York", False, "blog",
        task_name="Agent Media Pipeline"),
    Job("local:agenttrade-sideload-amd", "AgentTrade Sideload AMD", "local",
        "weekly", "America/New_York", False, "trading",
        task_name="AgentTradeSideloadAMD"),
    Job("local:dexter-weekly-orchestrator", "Dexter Weekly Orchestrator",
        "local", "weekly", "America/New_York", False, "blog",
        task_name="Dexter Weekly Orchestrator"),
    Job("local:dexter-trader-runner", "DexterTrader Runner", "local",
        "weekly", "America/New_York", False, "trading",
        task_name="DexterTraderRunner"),
    Job("local:dexter-trader-stopper", "DexterTrader Stopper", "local",
        "weekly", "America/New_York", False, "trading",
        task_name="DexterTraderStopper"),
    Job("local:dexter-weekly-optimization", "DexterTrader Weekly Optimization",
        "local", "weekly", "America/New_York", False, "trading",
        task_name="DexterTrader_Weekly_Optimization"),
    Job("local:eod-tasks", "EOD Tasks", "local",
        "weekly", "America/New_York", False, "trading",
        task_name="EOD Tasks"),
)


# ---------------------------------------------------------------------------
# CLOUD -- Cloud Scheduler, each triggering a Cloud Run job
# ---------------------------------------------------------------------------
# `target` is the Cloud Run job name. The collector uses it to pull execution
# history, so the scheduler row and the execution row become one job.
CLOUD_JOBS: tuple[Job, ...] = (
    Job("cloud:agent-trade", "Agent Trade", "scheduler",
        "every 15 min", "America/New_York", True, "trading",
        target="agent-trade-job",
        success_rule={"execution_status": ("==", "Completed")}),

    Job("cloud:agent-trade-health", "Agent Trade Health", "scheduler",
        "18:00 Mon-Fri", "America/New_York", True, "trading",
        target="agent-trade-health",
        success_rule={"execution_status": ("==", "Completed")}),

    Job("cloud:swing-open", "Swing Open", "scheduler",
        "09:35 Mon-Fri", "America/New_York", True, "trading",
        target="swing-trader",
        success_rule={"execution_status": ("==", "Completed")}),

    Job("cloud:swing-intraday", "Swing Intraday", "scheduler",
        "every 15 min 09-16 Mon-Fri", "America/New_York", True, "trading",
        target="swing-trader",
        success_rule={"execution_status": ("==", "Completed")}),

    Job("cloud:swing-eod", "Swing EOD", "scheduler",
        "16:05 Mon-Fri", "America/New_York", True, "trading",
        target="swing-trader",
        success_rule={"execution_status": ("==", "Completed")}),

    Job("cloud:swing-opg-staging", "Swing OPG Staging", "scheduler",
        "19:05 Mon-Fri", "America/New_York", True, "trading",
        target="swing-trader",
        success_rule={"execution_status": ("==", "Completed")}),

    Job("cloud:swing-fill-audit", "Swing Fill Audit", "scheduler",
        "16:30 Fri", "America/New_York", True, "trading",
        target="swing-fill-audit",
        success_rule={"execution_status": ("==", "Completed")}),

    Job("cloud:sideload-trader", "Sideload Trader", "scheduler",
        "every 15 min 09-16 Mon-Fri", "America/New_York", True, "trading",
        target="sideload-trader",
        success_rule={"execution_status": ("==", "Completed")}),

    Job("cloud:sideload-daily", "Sideload Daily", "scheduler",
        "01:30 UTC daily", "Etc/UTC", False, "trading",
        target="sideload-daily",
        success_rule={"execution_status": ("==", "Completed")},
        notes="PAUSED. Only paused scheduler out of 17."),

    Job("cloud:flatbase-scan", "Flatbase Scan", "scheduler",
        "15:45 Mon-Fri", "America/New_York", True, "trading",
        target="flatbase-runner",
        success_rule={"execution_status": ("==", "Completed")}),

    Job("cloud:flatbase-monitor", "Flatbase Monitor", "scheduler",
        "hourly 10-15 Mon-Fri", "America/New_York", True, "trading",
        target="flatbase-runner",
        success_rule={"execution_status": ("==", "Completed")}),

    Job("cloud:flatbase-fill-audit", "Flatbase Fill Audit", "scheduler",
        "16:30 Fri", "America/New_York", True, "trading",
        target="flatbase-fill-audit",
        success_rule={"execution_status": ("==", "Completed")}),

    Job("cloud:model-a-options", "Model A Options", "scheduler",
        "09:25 Mon-Fri", "America/New_York", True, "trading",
        target="model-a-options-runner",
        success_rule={"execution_status": ("==", "Completed")}),

    Job("cloud:model-a-options-db-archive", "Model A Options DB Archive",
        "scheduler", "17:00 Mon-Fri", "America/New_York", True, "trading",
        target="model-a-options-db-archive",
        success_rule={"execution_status": ("==", "Completed")}),

    Job("cloud:dexter-blog", "Dexter Blog", "scheduler",
        "16:30 Mon-Fri", "America/New_York", True, "blog",
        target="dexter-blog-update",
        success_rule={"execution_status": ("==", "Completed")}),

    Job("cloud:ticker-roster-daily", "Ticker Roster Daily", "scheduler",
        "20:00 daily", "America/New_York", True, "infra",
        target="ticker-roster-daily",
        success_rule={"execution_status": ("==", "Completed")}),

    Job("cloud:ticker-roster-weekly", "Ticker Roster Weekly", "scheduler",
        "20:00 Fri", "America/New_York", True, "infra",
        target="ticker-roster-weekly",
        success_rule={"execution_status": ("==", "Completed")}),
)


ALL_JOBS: tuple[Job, ...] = LOCAL_JOBS + CLOUD_JOBS
BY_ID: dict[str, Job] = {j.job_id: j for j in ALL_JOBS}

CATEGORIES = tuple(sorted({j.category for j in ALL_JOBS}))
SOURCES = ("local", "scheduler", "runjob")


def get(job_id: str) -> Job:
    """Look up a job. Raises KeyError with a useful message."""
    try:
        return BY_ID[job_id]
    except KeyError:
        raise KeyError(
            f"unknown job {job_id!r}; known: {sorted(BY_ID)}") from None


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------
_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
}


def evaluate_rule(job: Job, detail: dict) -> tuple[bool | None, str]:
    """Apply a job's success rule to a run's detail.

    Returns (passed, explanation). `passed` is None when the rule cannot be
    evaluated -- a missing field, or no rule at all. None is deliberately
    distinct from False: "we could not tell" must not be reported as "it
    failed", and it must not be reported as "it worked" either.
    """
    if not job.success_rule:
        return None, "no success rule (exit code only)"

    for field_name, (op, expected) in job.success_rule.items():
        if field_name not in detail:
            return None, f"rule needs {field_name!r}, not in detail"
        actual = detail[field_name]
        fn = _OPS.get(op)
        if fn is None:
            return None, f"unknown operator {op!r}"
        try:
            if not fn(actual, expected):
                return False, f"{field_name}={actual!r} fails {op} {expected!r}"
        except TypeError:
            return None, f"cannot compare {field_name}={actual!r} {op} {expected!r}"
    return True, "rule passed"


def validate() -> list[str]:
    """Check the registry for mistakes. Returns a list of problems.

    Called at import time by the collector so a typo in a rule fails loudly
    instead of silently marking every run unknown.
    """
    problems: list[str] = []
    seen: set[str] = set()
    for j in ALL_JOBS:
        if j.job_id in seen:
            problems.append(f"duplicate job_id {j.job_id!r}")
        seen.add(j.job_id)
        if j.source not in SOURCES:
            problems.append(f"{j.job_id}: bad source {j.source!r}")
        if "/" not in j.timezone:
            problems.append(f"{j.job_id}: timezone {j.timezone!r} looks wrong")
        for fname, (op, _exp) in j.success_rule.items():
            if op not in _OPS:
                problems.append(f"{j.job_id}: unknown operator {op!r}")
    return problems
