"""The JSON the dashboard serves.

READ-ONLY BY DESIGN
-------------------
Nothing here writes. The collector is the only writer, so a slow or broken
API cannot corrupt history and a slow collector cannot make the page hang.

ENDPOINTS
---------
    GET /api/jobs                  every job + its latest run
    GET /api/jobs/history          runs in a window, filterable
    GET /api/jobs/collect          run a collection pass now (POST-ish, but
                                   GET so a browser refresh can trigger it)

QUERY PARAMETERS
----------------
    since, until    ISO dates or 'YYYY-MM-DD'. Defaults to the last 7 days.
    job             repeatable; filter to specific job_ids
    category        trading | blog | infra | research
    source          local | scheduler | runjob
    outcome         ok | no_op | failed | missed | skipped | running | unknown
    include_disabled  1 to include disabled/paused jobs (default 1)
    limit           max history rows (default 500)

TIMEZONE
--------
Everything is stored in UTC and returned in UTC. The page converts to local
for display. Doing the conversion in one place (the browser) avoids the class
of bug where a job at 20:00 New York appears on the wrong day because the
server was thinking in UTC.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from jobwatch import registry, store


def _parse_when(v: str | None, default: datetime) -> str:
    """Accept 'YYYY-MM-DD' or a full ISO timestamp. Returns ISO UTC."""
    if not v:
        return default.isoformat()
    v = v.strip()
    try:
        if len(v) == 10:
            dt = datetime.fromisoformat(v).replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return default.isoformat()


def _job_payload(job: registry.Job, run: dict | None,
                 include_log: bool = False) -> dict:
    """One job plus its latest run, shaped for the page.

    `include_log` is False by default. The log excerpt is the single biggest
    field -- 32 jobs x 50 lines made the payload 717 KB, which is slow to
    parse and pointless to send when the page only shows it after a click.
    The detail view fetches it separately.
    """
    out = {
        "job_id": job.job_id,
        "name": job.name,
        "source": job.source,
        "schedule": job.schedule,
        "timezone": job.timezone,
        "enabled": job.enabled,
        "category": job.category,
        "target": job.target,
        "has_rule": bool(job.success_rule),
        "scheduled": job.scheduled,
        "notes": job.notes,
        "log_hint": job.log_hint,
        "outcome": registry.UNKNOWN,
        "did_work": None,
        "started_at": None,
        "finished_at": None,
        "duration_s": None,
        "exit_code": None,
        "detail": {},
        "log_excerpt": None,
        "rule_note": None,
    }
    if run:
        out.update({
            "outcome": run.get("outcome") or registry.UNKNOWN,
            "did_work": run.get("did_work"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "duration_s": run.get("duration_s"),
            "exit_code": run.get("exit_code"),
            "detail": run.get("detail") or {},
            "rule_note": (run.get("detail") or {}).get("rule"),
        })
        if include_log:
            out["log_excerpt"] = run.get("log_excerpt")
    return out


def jobs_payload(params: dict) -> dict:
    """The main payload: every job with its latest run."""
    # In the container the DB is a read-only copy pulled from GCS. The TTL
    # inside download_from_gcs keeps this from re-downloading on every poll.
    store.download_from_gcs()
    latest = store.latest_per_job()
    jobs = [_job_payload(j, latest.get(j.job_id)) for j in registry.ALL_JOBS]
    # filters
    include_disabled = params.get("include_disabled", "1") != "0"
    if not include_disabled:
        jobs = [j for j in jobs if j["enabled"]]

    cat = params.get("category")
    if cat:
        jobs = [j for j in jobs if j["category"] == cat]

    src = params.get("source")
    if src:
        jobs = [j for j in jobs if j["source"] == src]

    outcome = params.get("outcome")
    if outcome:
        jobs = [j for j in jobs if j["outcome"] == outcome]

    q = (params.get("q") or "").strip().lower()
    if q:
        jobs = [j for j in jobs
                if q in j["name"].lower() or q in j["job_id"].lower()]

    # summary counts, computed BEFORE the outcome filter so the header always
    # shows the true state of the world
    all_jobs = [_job_payload(j, latest.get(j.job_id))
                for j in registry.ALL_JOBS]
    counts: dict[str, int] = {}
    for j in all_jobs:
        counts[j["outcome"]] = counts.get(j["outcome"], 0) + 1

    attention = [j for j in all_jobs
                 if j["outcome"] in registry.ATTENTION and j["enabled"]
                 and j["scheduled"]]

    last = store.last_collect()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "last_collect": last,
        "counts": counts,
        "attention": [
            {"job_id": j["job_id"], "name": j["name"],
             "outcome": j["outcome"], "started_at": j["started_at"],
             "rule_note": j["rule_note"]}
            for j in attention
        ],
        "total_jobs": len(all_jobs),
        "shown": len(jobs),
        "jobs": jobs,
        "categories": list(registry.CATEGORIES),
        "sources": list(registry.SOURCES),
        "outcomes": list(registry.OUTCOMES),
    }


def history_payload(params: dict) -> dict:
    """Run history in a window."""
    now = datetime.now(timezone.utc)
    since = _parse_when(params.get("since"), now - timedelta(days=7))
    until = _parse_when(params.get("until"), now)

    job_ids = params.get("job")
    if isinstance(job_ids, str):
        job_ids = [job_ids]
    if job_ids:
        job_ids = [j for j in job_ids if j in registry.BY_ID]

    limit = int(params.get("limit") or 500)
    rows = store.runs_between(since, until, job_ids or None, limit=limit)

    outcome = params.get("outcome")
    if outcome:
        rows = [r for r in rows if r.get("outcome") == outcome]

    return {
        "since": since,
        "until": until,
        "count": len(rows),
        "counts": store.outcome_counts(since, until),
        "runs": [
            {
                "job_id": r["job_id"],
                "name": registry.BY_ID[r["job_id"]].name
                        if r["job_id"] in registry.BY_ID else r["job_id"],
                "outcome": r.get("outcome"),
                "did_work": r.get("did_work"),
                "started_at": r.get("started_at"),
                "finished_at": r.get("finished_at"),
                "duration_s": r.get("duration_s"),
                "exit_code": r.get("exit_code"),
                "detail": r.get("detail") or {},
                "log_excerpt": r.get("log_excerpt"),
            }
            for r in rows
        ],
    }


def collect_now() -> dict:
    """Run a collection pass. Used by the page's Refresh button.

    In the Cloud Run container this CANNOT work: the collector shells out to
    `schtasks` (Windows-only) and `gcloud` (not installed in the image). So
    the container reports that honestly instead of returning a cheerful
    "collected 0 runs", which would look like a quiet day.

    The container's job is to READ the DB the Windows machine uploaded. The
    Refresh button there re-downloads from GCS rather than collecting.
    """
    import os
    import sys

    in_container = (sys.platform != "win32"
                    or os.getenv("K_SERVICE") is not None)
    if in_container:
        pulled = store.download_from_gcs(max_age_s=0)
        return {
            "mode": "read_only",
            "pulled_from_gcs": pulled,
            "message": ("This deployment cannot collect: the collector needs "
                        "Windows Task Scheduler and the gcloud CLI. Showing "
                        "the most recent data uploaded by the local machine."),
        }

    from jobwatch import collect
    result = collect.collect_all(verbose=False)
    result["mode"] = "collected"
    return result


def job_detail_payload(params: dict) -> dict:
    """One job's latest run including the log excerpt.

    Split out from `jobs_payload` because the excerpt is large and is only
    needed when the user opens a row. Sending 32 excerpts up front made the
    main payload 717 KB.
    """
    job_id = params.get("job_id") or ""
    if job_id not in registry.BY_ID:
        return {"error": f"unknown job {job_id!r}"}
    job = registry.BY_ID[job_id]
    latest = store.latest_per_job().get(job_id)
    payload = _job_payload(job, latest, include_log=True)
    payload["history"] = [
        {
            "outcome": r.get("outcome"),
            "started_at": r.get("started_at"),
            "duration_s": r.get("duration_s"),
            "exit_code": r.get("exit_code"),
            "detail": r.get("detail") or {},
        }
        for r in store.runs_for(job_id, limit=20)
    ]
    return payload


def dispatch(path: str, params: dict) -> tuple[int, dict]:
    """Route a request. Returns (status, payload)."""
    try:
        if path == "/api/jobs":
            return 200, jobs_payload(params)
        if path == "/api/jobs/history":
            return 200, history_payload(params)
        if path == "/api/jobs/detail":
            return 200, job_detail_payload(params)
        if path == "/api/jobs/collect":
            return 200, collect_now()
        return 404, {"error": f"unknown path {path}"}
    except Exception as e:
        # Never let an exception become a blank page. The error is returned
        # as JSON so the page can show it instead of hanging.
        return 500, {"error": f"{type(e).__name__}: {e}"}
