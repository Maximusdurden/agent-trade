"""Push job failures to Discord.

WHY THIS IS SEPARATE FROM THE PAGE
----------------------------------
The page is pull: you have to look at it. A job that fails at 20:00 and is
still failing at 09:00 the next morning is exactly the case the page cannot
solve, because nobody opened the tab. This module is the push half.

WHAT COUNTS AS A FAILURE
------------------------
`failed` and `missed` only. Deliberately NOT `no_op`.

`no_op` means the job ran, exited 0, and did nothing -- usually because the
upstream data was not ready yet (Kalshi had not listed the slate). That is
worth showing on the page, where you can see it in context next to the
schedule, but it is not worth waking you up. Alerting on it would train you
to ignore the channel, which is how alerting dies.

If you want `no_op` in Discord too, pass it in `outcomes`.

WHY DEDUPE LIVES IN THE DATABASE
--------------------------------
The collector runs hourly and re-reads overlapping windows, so the same
failed run is seen again on every pass. `job_runs.notified_at` is the
memory: a run is alerted once, then stamped. Without it a single failure
would page you every hour until you fixed it.

The stamp is written only AFTER a successful send. If Discord is down, the
run stays unalerted and the next pass retries -- which is the behaviour you
want, and the opposite of what "mark first, send after" would give you.

WHY IT NEVER RAISES
-------------------
A Discord outage must not fail a collection pass. The data is already
correct and stored; only the notification is missing, and the next pass
retries it.
"""
from __future__ import annotations

import os
from datetime import datetime

from jobwatch import registry, store

# Failures worth a notification. See the module docstring for why no_op is
# excluded.
ALERT_OUTCOMES = (registry.FAILED, registry.MISSED)

# Discord rejects content over 2000 characters. Leave room for the header.
MAX_CONTENT = 1900

# How many runs to put in one message before summarising the rest.
MAX_LINES = 10


def _webhook_url() -> str:
    """The webhook, from the environment or the project's .env.

    `core.config` already calls `load_dotenv(override=True)`, so importing it
    is enough to pick up agent-trade/.env. Reading os.getenv directly keeps
    this module importable without pulling in the trading config.
    """
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if url:
        return url
    try:
        from core import config  # noqa: F401  (import loads .env)
        return (getattr(config, "DISCORD_WEBHOOK_URL", "") or "").strip()
    except Exception:
        return ""


def _fmt_when(iso: str | None) -> str:
    """UTC timestamp -> compact readable form. No timezone guessing."""
    if not iso:
        return "unknown time"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso


def _line(run: dict) -> str:
    """One failure, as a single Discord line."""
    job = registry.BY_ID.get(run["job_id"])
    name = job.name if job else run["job_id"]
    bits = [f"**{name}**", f"`{run.get('outcome')}`"]

    when = _fmt_when(run.get("started_at") or run.get("scheduled_for"))
    bits.append(when)

    if run.get("exit_code") is not None:
        bits.append(f"exit {run['exit_code']}")

    # The rule explanation is the most useful field: it says WHY the run was
    # judged a failure, not just that it was. It goes on its own line because
    # it is usually the longest part and reads badly inline.
    detail = run.get("detail") or {}
    why = detail.get("rule")
    line = " · ".join(bits)
    if why and why not in ("rule passed", "no success rule (exit code only)"):
        line += f"\n    ↳ {why}"
    return line


def build_message(runs: list[dict], total: int | None = None) -> str:
    """Compose the Discord content for a batch of failed runs."""
    total = len(runs) if total is None else total
    plural = "" if total == 1 else "s"
    head = f"🚨 **JobWatch: {total} job failure{plural}**"
    lines = [head, ""]
    for r in runs[:MAX_LINES]:
        lines.append("• " + _line(r))
    if total > MAX_LINES:
        lines.append(f"…and {total - MAX_LINES} more. "
                     f"See https://dashboard.agenttrade.us/jobs")
    elif total > 1:
        lines.append("")
        lines.append("https://dashboard.agenttrade.us/jobs")

    text = "\n".join(lines)
    if len(text) > MAX_CONTENT:
        text = text[:MAX_CONTENT - 20] + "\n…(truncated)"
    return text


def _post(content: str) -> bool:
    """Send one message. Returns True only on a 2xx."""
    url = _webhook_url()
    if not url:
        return False
    try:
        import requests
        resp = requests.post(url, json={"content": content}, timeout=15)
        return 200 <= resp.status_code < 300
    except Exception:
        return False


def alertable_job_ids() -> list[str]:
    """Jobs whose failure is worth a notification.

    Enabled and scheduled only. A logon-triggered task is excluded because
    its last result is not a statement about a schedule -- `Master MCP
    Gateway` reports exit 0xC000013A (STATUS_CONTROL_C_EXIT) every time the
    machine shuts down, which is the process being killed at logoff, not a
    failure. Alerting on it would page on every reboot.

    This mirrors the page's attention list, which applies the same two
    filters. If the two ever disagree, the page and Discord tell different
    stories about the same system, which is worse than either being wrong.
    """
    return [j.job_id for j in registry.ALL_JOBS if j.enabled and j.scheduled]


def notify_failures(outcomes: tuple[str, ...] = ALERT_OUTCOMES,
                    dry_run: bool = False) -> dict:
    """Alert on any unalerted failure. Returns a summary dict.

    `dry_run` builds the message and reports what it would send without
    posting or stamping -- so you can check the wording without spamming a
    channel or burning the dedupe stamp.
    """
    runs = store.unnotified_attention(outcomes, alertable_job_ids())
    if not runs:
        return {"sent": False, "count": 0, "reason": "nothing to alert"}

    content = build_message(runs)

    if dry_run:
        return {"sent": False, "count": len(runs), "dry_run": True,
                "content": content}

    if not _webhook_url():
        return {"sent": False, "count": len(runs),
                "reason": "DISCORD_WEBHOOK_URL not set"}

    if not _post(content):
        # Do NOT stamp. The next pass retries.
        return {"sent": False, "count": len(runs),
                "reason": "discord post failed"}

    stamped = store.mark_notified([r["run_key"] for r in runs])
    return {"sent": True, "count": len(runs), "stamped": stamped,
            "content": content}
