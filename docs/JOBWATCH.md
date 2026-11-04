# JobWatch — one view of every scheduled job

**Date:** 2026-11-04
**Question:** what is running, did it work, and what happened last Tuesday?

**Answer:** `http://<dashboard>/jobs`

---

## Why this exists

Jobs are spread across three systems that do not know about each other:

| System | Count | What it knows |
|---|---|---|
| Windows Task Scheduler | 15 (8 enabled) | local tasks, exit codes |
| Cloud Scheduler | 17 (16 enabled) | cron, last attempt |
| Cloud Run jobs | 13 | execution history |

Answering "did everything run today?" meant checking all three by hand. Worse,
**the three disagree about what "ok" means.**

On 2026-11-03 `NBA Prop Daily` reported `result=0` while recording
`markets: 0`. It ran, it exited cleanly, and it did nothing — because Kalshi
had not listed the slate yet. A report built on exit codes would have shown
that as a healthy green row.

That is the failure mode this project exists to catch.

---

## The five outcomes

A run is exactly one of:

| outcome | meaning | how it is detected |
|---|---|---|
| `ok` | ran, exited 0, and did something | exit code + success rule |
| `no_op` | ran, exited 0, and did nothing | **success rule failed** |
| `failed` | ran and exited non-zero | exit code |
| `missed` | was scheduled and never ran | inferred from schedule |
| `skipped` | disabled or paused, so not expected | task/scheduler state |

`no_op` and `missed` are the two states **no single source can report**, and
they are the two that matter. A job that quietly does nothing looks exactly
like a healthy job in every dashboard that exists today.

`did_work` is stored separately from `outcome` because they are not the same
question. A run can exit 0 and do nothing (`no_op`), and a run can fail after
doing real work.

---

## Success rules

Exit code 0 does not mean success, so each job carries a declarative rule
evaluated against the run's extracted detail:

```python
Job("local:nba-prop-daily", ..., success_rule={"markets": (">", 0)})
```

The collector extracts `markets` from `out/runlog.jsonl` and applies the rule.
A run with `markets: 0` becomes `no_op`, not `ok`.

**A rule that cannot be evaluated returns `None`, never `False` and never
`True`.** "We could not tell" must not be reported as "it failed", and must
not be reported as "it worked" either. Jobs with no rule fall back to
exit-code-only and are flagged as such in the UI, so the weaker guarantee is
visible.

---

## Architecture

```
registry.py    the job list + per-job success rule
store.py       SQLite: job_runs, one row per observed run
collect.py     the collector -- reads all three sources, writes rows
api.py         the JSON the dashboard serves
page.py        the HTML page
```

**The collector is the only writer. The API only reads.** A slow or broken
collector cannot make the page hang, and the page cannot corrupt history.

### Storage

SQLite at `data/jobwatch.db`, separate from `data/trades.db`. Job history is
operational metadata with a different retention policy and a different failure
mode — a corrupt job-history table must never be able to take down trading.

WAL mode so the API can read while the collector writes.

### Idempotency

The collector runs hourly over overlapping windows. `run_key` is a
deterministic hash of `(job_id, scheduled_for, source_ref)`, so re-collecting
the same run **updates** the row instead of duplicating it. Without this the
history would grow a duplicate every hour.

---

## The page

`/jobs` on the dashboard. No build step, no dependencies, one HTML string.

- **Header** — last collected timestamp, counts by outcome, and an
  **attention strip** listing only the jobs that are `no_op`, `failed`, or
  `missed`. A healthy system shows "All clear"; a broken one does not.
- **Filters** — search, category, source, outcome, and a *show disabled*
  checkbox (on by default, per the requirement to include disabled jobs).
- **Table** — one row per job: name, schedule, outcome, did-work, last run,
  rule note.
- **Detail** — click a row for the raw detail JSON, the last 50 log lines, and
  the last 20 runs.
- **History** — a date range picker that loads runs for the window.

### Refresh cadence

The page polls every **5 minutes** and has a **Refresh now** button that
triggers a collection pass immediately. The collector task runs **hourly**.

This was a deliberate change from the original "near real time" ask: cloud
jobs live ~90 seconds, so there is usually nothing live to show. A 10-second
poll would be noise. The last-collected timestamp is always visible so a stale
page is distinguishable from a quiet day.

### Timezone

Everything is stored and served in **UTC**. The browser converts to local for
display. Doing the conversion in one place avoids the class of bug where a
job at 20:00 New York appears on the wrong day because the server was thinking
in UTC.

---

## Security

Reuses the dashboard's cookie auth (`DASHBOARD_PASSWORD`).

**Log excerpts are redacted before storage.** A traceback that prints an API
key would otherwise leak it to anyone who can see the page. Patterns caught:

```
api_key=... / token: ... / password=... / bearer ...
sk-... / ghp_... / gho_... / AKIA... / xox[baprs]-...
-----BEGIN ... PRIVATE KEY-----
```

Redaction happens at collection time, not display time, so the secret never
reaches the database.

---

## Bugs found while building this

Each of these produced plausible-looking output, which is why they matter.

**1. `gcloud` is a `.ps1` shim.** `subprocess` cannot execute it — "command
not found" even though the shell finds it fine. Fixed by routing through
PowerShell with the call operator `&`. Without `&`, PowerShell parses the
first token as a string literal and the rest as a syntax error.

**2. PowerShell `$r += ...` in a `foreach` returned only the first task.**
The loop looked correct and silently produced one row instead of fifteen.

**3. `State` serialises as an int, not a string.** `'int' object has no
attribute 'lower'` crashed the entire local collection on the first run.

**4. `MAX()` self-join dropped every NULL timestamp.** 27 of 32 jobs vanished
from the page. Replaced with `ROW_NUMBER() OVER (PARTITION BY job_id)`.

**5. Registry used display names, the machine uses compact names.**
`"AgentTrade AMD Monitor"` vs `"AgentTradeAMDMonitor"` — every local job
reported "task not found". Fixed with an explicit `task_name` field.

**6. Cloud Scheduler names carry a `-scheduler` suffix** the registry job_id
does not. All 17 cloud jobs showed `skipped`.

**7. The main payload was 717 KB.** 32 jobs × 50 log lines. Split into a
detail endpoint; now 19.7 KB.

**8. A logon-triggered task was flagged `failed` forever.** `Master MCP
Gateway` runs on logon, so its last result reflects the last logon, not a
schedule. Added a `scheduled` flag and excluded it from the attention list.

**9. WAL sidecar files block deletion on Windows.** The test cleanup raised
`PermissionError` on `-wal` and `-shm`.

---

## Usage

```powershell
# collect once
python -m jobwatch.collect --verbose

# prune old history
python -m jobwatch.collect --prune-days 90

# tests
python tests/test_jobwatch.py
```

The scheduled task **JobWatch Collector** runs hourly and appends to
`logs/jobwatch_collect.log`.

---

## What is still missing

**`missed` is not yet inferred.** The outcome exists in the vocabulary and the
page renders it, but the collector does not compute it. Doing so needs the
schedule parsed into "when should this have run since the last success", which
is per-job work. Today a task that never ran because the laptop was asleep
shows as `unknown`, not `missed`.

**Cloud Run logs are not ingested.** The collector reads execution *status*
but not the job's stdout. Cloud Logging has it; pulling it would mean a
`gcloud logging read` per execution, which is slow and costs money at volume.
The detail view shows local log excerpts only.

**Retention is manual.** `--prune-days` exists but nothing calls it. At ~32
rows per hour the table grows ~280k rows/year, which is fine for SQLite but
should be pruned on a schedule eventually.
