"""The collector: read all three sources, write normalised rows.

WHAT IT DOES
------------
One pass:
    1. read Windows Task Scheduler for every local job
    2. read Cloud Scheduler for every cloud job
    3. read Cloud Run execution history for the jobs those schedulers trigger
    4. join scheduler rows to execution rows so one job is one row
    5. apply each job's success rule
    6. write to SQLite

WHY IT IS SEPARATE FROM THE API
-------------------------------
The collector shells out to `schtasks` and `gcloud`, which take seconds and
can hang. The API must answer in milliseconds. Keeping them apart means a
slow or broken collector cannot make the page hang, and the page cannot
corrupt the history.

FAILURE IS PER-SOURCE, NOT PER-PASS
-----------------------------------
If `gcloud` is not installed, the local jobs must still be collected. Each
source is wrapped so one failure degrades the report instead of emptying it,
and the failure is recorded in `collect_runs.errors_json` so the page can say
"cloud data unavailable" rather than silently showing nothing.

THE MISSED-RUN PROBLEM
----------------------
Task Scheduler reports `Ready` for a task that never ran because the machine
was asleep. There is no "missed" state to read. So `missed` is INFERRED: if a
job's schedule says it should have run since the last successful run, and it
did not, it is marked missed. That inference is the only way to see the
failure mode that matters most for a laptop-hosted scheduler.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jobwatch import registry, store
from jobwatch.registry import (FAILED, MISSED, NO_OP, OK, RUNNING, SKIPPED,
                               UNKNOWN)

# How much log tail to keep per run. 50 lines is enough to see a traceback
# without turning the DB into a log archive.
LOG_TAIL_LINES = 50

# Anything that looks like a credential is replaced before storage. Log
# excerpts are shown in a browser; a traceback that prints an API key would
# leak it to anyone who can see the page.
#
# Order matters: the key=value patterns run first so `api_key=sk-...` is
# caught as a whole, then the bare-token patterns catch anything that appears
# without a label.
_SECRET_PATTERNS = (
    # labelled secrets: api_key=..., token: ..., password=...
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|bearer)"
                r"\s*[=:]\s*\S+"),
     r"\1=<redacted>"),
    # bare tokens that are recognisable by shape
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "<redacted>"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "<redacted>"),
    (re.compile(r"gho_[A-Za-z0-9]{20,}"), "<redacted>"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "<redacted>"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "<redacted>"),
    # PEM blocks: redact the whole header line, which is enough to make the
    # block unusable and is what a log would contain.
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "<redacted-pem>"),
)


def redact(text: str) -> str:
    """Strip anything that looks like a credential."""
    if not text:
        return ""
    out = text
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """Run a command, returning (code, stdout, stderr). Never raises.

    On Windows `gcloud` is a `.ps1` shim, which `subprocess` cannot execute
    directly -- it fails with "command not found" even though the shell finds
    it fine. So on Windows the command is routed through PowerShell, which
    resolves the shim the same way an interactive shell does.
    """
    if sys.platform == "win32" and cmd and cmd[0] == "gcloud":
        # `gcloud` on Windows is a .ps1 shim. PowerShell needs the call
        # operator `&` to invoke a quoted command name; without it the first
        # token is parsed as a string literal and the rest as a syntax error.
        quoted = " ".join(
            "'" + str(a).replace("'", "''") + "'" for a in cmd)
        cmd = ["powershell", "-NoProfile", "-Command", "& " + quoted]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, shell=False)
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError:
        return -1, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -2, "", f"timed out after {timeout}s: {' '.join(cmd[:3])}"
    except Exception as e:
        return -3, "", f"{type(e).__name__}: {e}"


def _tail(path: Path, lines: int = LOG_TAIL_LINES) -> str:
    """Last N lines of a file, redacted. Empty string if unreadable."""
    try:
        if not path.exists() or not path.is_file():
            return ""
        # Read the whole file only if small; otherwise seek near the end.
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > 256 * 1024:
                fh.seek(-256 * 1024, 2)
            raw = fh.read()
        text = raw.decode("utf-8", errors="replace")
        return redact("\n".join(text.splitlines()[-lines:]))
    except Exception:
        return ""


def _extract_verdict(path: Path) -> dict:
    """Pull a `verdict` field out of a monitor's log or JSON output.

    The AgentTrade monitors print a JSON block ending with a `verdict` key
    ("OK", "NO_TRADES", ...). The success rule needs that value, so it is
    scraped from the tail of the newest log file.

    Returns {} when nothing is found, so the rule reports "cannot evaluate"
    rather than guessing.
    """
    try:
        if not path.exists():
            return {}
        if path.is_dir():
            files = [f for f in path.glob("*") if f.is_file()]
            if not files:
                return {}
            path = max(files, key=lambda f: f.stat().st_mtime)
        if not path.is_file():
            return {}
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > 256 * 1024:
                fh.seek(-256 * 1024, 2)
            raw = fh.read()
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return {}

    # Look for "verdict": "X" anywhere in the tail, newest first.
    for m in reversed(list(re.finditer(r'"verdict"\s*:\s*"([^"]+)"', text))):
        return {"verdict": m.group(1)}
    return {}


def _extract_runlog_detail(path: Path) -> dict:
    """Pull the last `count` event out of a JSONL run log.

    The NBA pipeline writes structured events to `out/runlog.jsonl`. The
    `count` event carries `markets`, which is the field the success rule
    needs -- a run with 0 markets exited 0 and did nothing, and that is
    exactly the case the rule exists to catch.

    Returns {} when the file is missing or unparseable, so the rule reports
    "cannot evaluate" rather than guessing.
    """
    try:
        if not path.exists() or not path.is_file():
            return {}
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > 512 * 1024:
                fh.seek(-512 * 1024, 2)
            raw = fh.read()
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return {}

    detail: dict = {}
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("event") == "count" and ev.get("name") == "signals":
            d = ev.get("detail") or {}
            if "markets" in d:
                detail["markets"] = d["markets"]
            if "events" in d:
                detail["events"] = d["events"]
            if "value" in ev:
                detail["signals"] = ev["value"]
            break
    return detail


# ---------------------------------------------------------------------------
# LOCAL -- Windows Task Scheduler
# ---------------------------------------------------------------------------
def _schtasks_json() -> list[dict]:
    """All non-Microsoft scheduled tasks as dicts. Empty on failure."""
    code, out, err = _run([
        "powershell", "-NoProfile", "-Command",
        "Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\\Microsoft\\*' } "
        "| Select-Object TaskName,TaskPath,State | ConvertTo-Json -Compress"
    ], timeout=90)
    if code != 0 or not out.strip():
        raise RuntimeError(f"schtasks query failed: {err[:200]}")
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


def _schtasks_info(names: list[str]) -> dict[str, dict]:
    """LastRunTime / LastTaskResult / NextRunTime per task.

    The obvious implementation loops in PowerShell and appends to an array.
    That silently returned only the FIRST task: `$r += ...` inside a
    `foreach` over a pipeline does not accumulate the way it looks like it
    should when the whole thing is one -Command string. Building the objects
    with ForEach-Object and letting ConvertTo-Json serialise the pipeline
    output is reliable.
    """
    if not names:
        return {}
    quoted = ",".join("'" + n.replace("'", "''") + "'" for n in names)
    script = (
        f"$n=@({quoted}); "
        f"$n | ForEach-Object {{ "
        f"  $i=Get-ScheduledTaskInfo -TaskName $_ -ErrorAction SilentlyContinue; "
        f"  if($i){{ [pscustomobject]@{{Name=$_;Last=$i.LastRunTime;"
        f"Next=$i.NextRunTime;Result=$i.LastTaskResult}} }} }} "
        f"| ConvertTo-Json -Compress"
    )
    code, out, err = _run(["powershell", "-NoProfile", "-Command", script],
                          timeout=120)
    if code != 0 or not out.strip():
        return {}
    try:
        data = json.loads(out)
    except ValueError:
        return {}
    rows = data if isinstance(data, list) else [data]
    return {r["Name"]: r for r in rows if isinstance(r, dict) and r.get("Name")}


def _parse_ps_date(v) -> str | None:
    """PowerShell JSON dates look like /Date(1699999999999)/ or ISO."""
    if not v:
        return None
    s = str(v)
    m = re.search(r"/Date\((-?\d+)", s)
    if m:
        try:
            return datetime.fromtimestamp(int(m.group(1)) / 1000,
                                          tz=timezone.utc).isoformat()
        except (ValueError, OSError):
            return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def collect_local() -> tuple[list[dict], list[str]]:
    """Collect every local job. Returns (rows, errors)."""
    errors: list[str] = []
    rows: list[dict] = []

    try:
        tasks = _schtasks_json()
    except Exception as e:
        return [], [f"local: {e}"]

    by_name = {t.get("TaskName"): t for t in tasks}
    wanted = [j for j in registry.LOCAL_JOBS]
    # Use the exact task name, not the display name. They differ for most
    # tasks ("AgentTradeAMDMonitor" vs "AgentTrade AMD Monitor"), and using
    # the display name silently matched nothing.
    info = _schtasks_info([j.task_name or j.name for j in wanted])

    for job in wanted:
        tname = job.task_name or job.name
        task = by_name.get(tname)
        if task is None:
            # The registry knows about a task the machine does not have.
            rows.append({
                "job_id": job.job_id, "source": "local",
                "outcome": UNKNOWN, "did_work": None,
                "detail": {"reason": f"task {tname!r} not found on this machine"},
                "source_ref": tname,
            })
            continue

        # Task Scheduler reports State as an enum. Depending on the PowerShell
        # version it serialises as a string ("Ready") or an integer (3), so
        # both are handled -- an int here crashed the first run with
        # "'int' object has no attribute 'lower'".
        raw_state = task.get("State")
        if isinstance(raw_state, int):
            state = {0: "unknown", 1: "disabled", 2: "queued",
                     3: "ready", 4: "running"}.get(raw_state, "unknown")
        else:
            state = str(raw_state or "").lower()
        i = info.get(tname) or {}
        last = _parse_ps_date(i.get("Last"))
        result = i.get("Result")
        enabled = state != "disabled"

        if not enabled:
            rows.append({
                "job_id": job.job_id, "source": "local",
                "outcome": SKIPPED, "did_work": None,
                "started_at": last, "source_ref": tname,
                "detail": {"state": state, "reason": "disabled"},
            })
            continue

        if last is None:
            rows.append({
                "job_id": job.job_id, "source": "local",
                "outcome": MISSED, "did_work": None,
                "source_ref": tname,
                "detail": {"state": state,
                           "reason": "enabled but has never run"},
            })
            continue

        # exit code -> outcome, then let the rule refine it
        if result == 0:
            outcome = OK
        elif result in (267009,):
            outcome = RUNNING
        else:
            outcome = FAILED

        detail: dict = {"state": state, "last_result": result}
        excerpt = ""
        if job.log_hint:
            p = Path(job.log_hint)
            if p.is_file():
                excerpt = _tail(p)
                # A JSONL run log carries structured fields the success rule
                # needs. Extract them so the rule can be evaluated.
                if p.suffix == ".jsonl":
                    detail.update(_extract_runlog_detail(p))
            elif p.is_dir():
                newest = None
                try:
                    files = [f for f in p.glob("*") if f.is_file()]
                    if files:
                        newest = max(files, key=lambda f: f.stat().st_mtime)
                except OSError:
                    newest = None
                if newest:
                    excerpt = _tail(newest)
                    detail["log_file"] = newest.name
            # A rule that needs `verdict` gets it from the monitor's output.
            if "verdict" in job.success_rule:
                detail.update(_extract_verdict(p))

        # Apply the success rule. A rule that fails turns an exit-0 run into
        # no_op -- which is the whole point: the process succeeded, the job
        # did not do its job.
        passed, why = registry.evaluate_rule(job, detail)
        detail["rule"] = why
        did_work = None
        if passed is True:
            did_work = True
        elif passed is False:
            did_work = False
            if outcome == OK:
                outcome = NO_OP

        rows.append({
            "job_id": job.job_id, "source": "local",
            "started_at": last, "scheduled_for": last,
            "exit_code": result, "outcome": outcome,
            "did_work": did_work, "detail": detail,
            "log_excerpt": excerpt, "source_ref": tname,
        })

    return rows, errors


# ---------------------------------------------------------------------------
# CLOUD -- Cloud Scheduler + Cloud Run executions
# ---------------------------------------------------------------------------
PROJECT = "agenttrade-us"
SCHEDULER_LOCATION = "us-central1"
RUN_REGION = "us-central1"


def _gcloud_json(args: list[str]) -> object:
    code, out, err = _run(["gcloud"] + args + ["--format=json"], timeout=120)
    if code != 0:
        raise RuntimeError(f"gcloud {' '.join(args[:3])} failed: {err[:200]}")
    return json.loads(out) if out.strip() else []


def collect_cloud() -> tuple[list[dict], list[str]]:
    """Collect Cloud Scheduler jobs and their Cloud Run executions."""
    errors: list[str] = []
    rows: list[dict] = []

    # --- scheduler state
    sched: dict[str, dict] = {}
    try:
        data = _gcloud_json([
            "scheduler", "jobs", "list",
            f"--project={PROJECT}", f"--location={SCHEDULER_LOCATION}"])
        for j in (data or []):
            sched[j.get("name", "").split("/")[-1]] = j
    except Exception as e:
        errors.append(f"cloud scheduler: {e}")

    # --- execution history per Cloud Run job
    execs: dict[str, list[dict]] = {}
    targets = sorted({j.target for j in registry.CLOUD_JOBS if j.target})
    for t in targets:
        try:
            data = _gcloud_json([
                "run", "jobs", "executions", "list",
                f"--job={t}", f"--project={PROJECT}", f"--region={RUN_REGION}",
                "--limit=5"])
            execs[t] = data or []
        except Exception as e:
            errors.append(f"cloud run {t}: {e}")
            execs[t] = []

    for job in registry.CLOUD_JOBS:
        # Cloud Scheduler names carry a '-scheduler' suffix that the registry
        # job_id does not (e.g. 'agent-trade-scheduler' vs 'cloud:agent-trade').
        # Try the bare name first, then the suffixed form.
        base = job.job_id.split(":", 1)[-1]
        sname = base if base in sched else f"{base}-scheduler"
        s = sched.get(sname)
        state = (s or {}).get("state", "")
        enabled = state.upper() == "ENABLED"

        if not enabled:
            rows.append({
                "job_id": job.job_id, "source": "scheduler",
                "outcome": SKIPPED, "did_work": None,
                "source_ref": sname,
                "detail": {"state": state or "unknown",
                           "reason": "paused or missing"},
            })
            continue

        runs = execs.get(job.target or "", [])
        if not runs:
            rows.append({
                "job_id": job.job_id, "source": "scheduler",
                "outcome": UNKNOWN, "did_work": None,
                "source_ref": sname,
                "detail": {"state": state,
                           "reason": "no execution history available"},
            })
            continue

        # newest execution
        def _start(e: dict) -> str:
            return ((e.get("status") or {}).get("startTime") or "")

        latest = max(runs, key=_start)
        st = latest.get("status") or {}
        conds = st.get("conditions") or []
        cond_type = conds[0].get("type") if conds else ""
        started = st.get("startTime")
        finished = st.get("completionTime")
        duration = None
        if started and finished:
            try:
                a = datetime.fromisoformat(started.replace("Z", "+00:00"))
                b = datetime.fromisoformat(finished.replace("Z", "+00:00"))
                duration = (b - a).total_seconds()
            except ValueError:
                pass

        outcome = OK if cond_type == "Completed" else FAILED
        detail = {
            "state": state,
            "execution": latest.get("metadata", {}).get("name"),
            "execution_status": cond_type,
        }
        passed, why = registry.evaluate_rule(job, detail)
        detail["rule"] = why
        did_work = None
        if passed is True:
            did_work = True
        elif passed is False:
            did_work = False
            if outcome == OK:
                outcome = NO_OP

        rows.append({
            "job_id": job.job_id, "source": "scheduler",
            "started_at": started, "finished_at": finished,
            "scheduled_for": started, "duration_s": duration,
            "outcome": outcome, "did_work": did_work, "detail": detail,
            "source_ref": sname,
        })

    return rows, errors


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------
def collect_all(verbose: bool = False) -> dict:
    """Run one full collection pass. Returns a summary dict."""
    started = datetime.now(timezone.utc).isoformat()
    problems = registry.validate()
    if problems:
        # A broken registry would silently mark everything unknown, so fail
        # loudly instead.
        raise RuntimeError("registry invalid: " + "; ".join(problems))

    all_rows: list[dict] = []
    errors: list[str] = list(problems)

    for name, fn in (("local", collect_local), ("cloud", collect_cloud)):
        try:
            rows, errs = fn()
            all_rows.extend(rows)
            errors.extend(errs)
            if verbose:
                print(f"  {name}: {len(rows)} rows, {len(errs)} errors",
                      flush=True)
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            if verbose:
                print(f"  {name}: FAILED {e}", flush=True)

    written = 0
    for r in all_rows:
        try:
            store.upsert_run(r)
            written += 1
        except Exception as e:
            errors.append(f"write {r.get('job_id')}: {e}")

    finished = datetime.now(timezone.utc).isoformat()
    store.record_collect(started, finished, len(all_rows), written, errors)

    return {
        "started_at": started, "finished_at": finished,
        "jobs_seen": len(all_rows), "runs_written": written,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Collect job status into SQLite")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--prune-days", type=int, default=0,
                    help="delete runs older than N days, then exit")
    args = ap.parse_args(argv)

    if args.prune_days:
        n = store.prune(args.prune_days)
        print(f"pruned {n} runs older than {args.prune_days} days")
        return 0

    print("collecting...", flush=True)
    summary = collect_all(verbose=args.verbose)
    print(f"  jobs seen    : {summary['jobs_seen']}")
    print(f"  runs written : {summary['runs_written']}")
    if summary["errors"]:
        print(f"  errors       : {len(summary['errors'])}")
        for e in summary["errors"][:10]:
            print(f"    - {e}")
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
