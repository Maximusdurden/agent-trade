"""SQLite storage for job runs.

WHY SQLITE AND NOT POSTGRES
---------------------------
The dashboard already uses SQLite (`core/database.py`), the write volume is
tiny (about 30 jobs x a few runs a day = hundreds of rows a day), and there is
exactly one writer. Postgres would add an ops burden for no benefit.

WHY A SEPARATE FILE
-------------------
`data/trades.db` is the trading database. Job history is operational metadata
with a different retention policy and a different failure mode -- a corrupt
job-history table must never be able to take down trading. So it lives in its
own file, `data/jobwatch.db`.

THE ONE TABLE THAT MATTERS
--------------------------
`job_runs` holds one row per observed run. The columns that carry the value
are `outcome` and `did_work`:

    outcome    ok | no_op | failed | missed | skipped | running | unknown
    did_work   1 if the run produced evidence of work, 0 if not, NULL if
               we could not tell

`did_work` is separate from `outcome` on purpose. A run can exit 0 and do
nothing (`no_op`), and a run can fail after doing real work. Collapsing them
loses the distinction that makes the report trustworthy.

IDEMPOTENCY
-----------
The collector runs hourly and re-reads overlapping windows. `run_key` is a
deterministic hash of (job_id, scheduled_for, source_ref), so re-collecting
the same run updates the row instead of duplicating it. Without this the
history would grow a duplicate every hour.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.config import BASE_DIR

DB_PATH = BASE_DIR / "data" / "jobwatch.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_runs (
    run_key        TEXT PRIMARY KEY,
    job_id         TEXT NOT NULL,
    source         TEXT NOT NULL,
    scheduled_for  TEXT,              -- ISO8601 UTC, when it was due
    started_at     TEXT,              -- ISO8601 UTC
    finished_at    TEXT,              -- ISO8601 UTC
    duration_s     REAL,
    exit_code      INTEGER,
    outcome        TEXT NOT NULL,
    did_work       INTEGER,           -- 1 | 0 | NULL
    detail_json    TEXT,              -- extracted fields, for the rule
    log_excerpt    TEXT,              -- last N lines, redacted
    source_ref     TEXT,              -- task name / execution name
    collected_at   TEXT NOT NULL,
    -- When a failure alert for this run was sent to Discord. NULL means
    -- "not yet alerted". The collector runs hourly over overlapping windows,
    -- so without this a single failure would page every hour forever.
    notified_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_job     ON job_runs(job_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON job_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_outcome ON job_runs(outcome);

-- One row per collector pass, so the page can show "last collected" and the
-- user can tell a stale page from a quiet day.
CREATE TABLE IF NOT EXISTS collect_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    jobs_seen    INTEGER DEFAULT 0,
    runs_written INTEGER DEFAULT 0,
    errors_json  TEXT
);
"""


def connect() -> sqlite3.Connection:
    """Open the jobwatch database, creating it if needed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL lets the API read while the collector writes. Without it a long
    # collect pass blocks the page.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that were introduced after a database already existed.

    `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so a new
    column in SCHEMA never reaches a database that was created before it.
    Without this, adding `notified_at` would raise "no such column" on the
    live DB while passing on a fresh one -- the worst kind of bug.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(job_runs)")}
    if "notified_at" not in cols:
        conn.execute("ALTER TABLE job_runs ADD COLUMN notified_at TEXT")


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_run_key(job_id: str, scheduled_for: str | None,
                 source_ref: str | None) -> str:
    """Deterministic key so re-collecting a run updates rather than duplicates."""
    raw = f"{job_id}|{scheduled_for or ''}|{source_ref or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def upsert_run(row: dict) -> None:
    """Insert or update one run. Idempotent on run_key."""
    init_db()
    key = row.get("run_key") or make_run_key(
        row["job_id"], row.get("scheduled_for"), row.get("source_ref"))
    detail = row.get("detail") or {}
    with connect() as conn:
        conn.execute("""
            INSERT INTO job_runs (
                run_key, job_id, source, scheduled_for, started_at,
                finished_at, duration_s, exit_code, outcome, did_work,
                detail_json, log_excerpt, source_ref, collected_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_key) DO UPDATE SET
                started_at   = excluded.started_at,
                finished_at  = excluded.finished_at,
                duration_s   = excluded.duration_s,
                exit_code    = excluded.exit_code,
                outcome      = excluded.outcome,
                did_work     = excluded.did_work,
                detail_json  = excluded.detail_json,
                log_excerpt  = excluded.log_excerpt,
                collected_at = excluded.collected_at
        """, (
            key, row["job_id"], row["source"], row.get("scheduled_for"),
            row.get("started_at"), row.get("finished_at"),
            row.get("duration_s"), row.get("exit_code"),
            row.get("outcome", "unknown"),
            None if row.get("did_work") is None else int(bool(row["did_work"])),
            json.dumps(detail) if detail else None,
            row.get("log_excerpt"), row.get("source_ref"), _now(),
        ))


def record_collect(started_at: str, finished_at: str, jobs_seen: int,
                   runs_written: int, errors: list[str]) -> None:
    init_db()
    with connect() as conn:
        conn.execute("""
            INSERT INTO collect_runs
                (started_at, finished_at, jobs_seen, runs_written, errors_json)
            VALUES (?,?,?,?,?)
        """, (started_at, finished_at, jobs_seen, runs_written,
              json.dumps(errors) if errors else None))


def last_collect() -> dict | None:
    init_db()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM collect_runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Queries used by the API
# ---------------------------------------------------------------------------
def latest_per_job() -> dict[str, dict]:
    """The most recent run for every job that has one.

    Uses ROW_NUMBER() rather than a MAX() self-join. The join version silently
    dropped every row whose timestamp was NULL -- which is most of them, since
    a disabled job has no start time -- so only 5 of 32 jobs appeared.
    """
    init_db()
    with connect() as conn:
        rows = conn.execute("""
            SELECT * FROM (
                SELECT r.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY job_id
                           ORDER BY COALESCE(started_at, scheduled_for,
                                             collected_at) DESC
                       ) AS rn
                FROM job_runs r
            ) WHERE rn = 1
        """).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["detail_json"]) if d["detail_json"] else {}
        out[d["job_id"]] = d
    return out


def runs_for(job_id: str, since: str | None = None,
             until: str | None = None, limit: int = 200) -> list[dict]:
    """Run history for one job, newest first."""
    init_db()
    sql = "SELECT * FROM job_runs WHERE job_id = ?"
    params: list = [job_id]
    if since:
        sql += " AND COALESCE(started_at, scheduled_for) >= ?"
        params.append(since)
    if until:
        sql += " AND COALESCE(started_at, scheduled_for) <= ?"
        params.append(until)
    sql += " ORDER BY COALESCE(started_at, scheduled_for) DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["detail_json"]) if d["detail_json"] else {}
        out.append(d)
    return out


def runs_between(since: str, until: str, job_ids: list[str] | None = None,
                 limit: int = 2000) -> list[dict]:
    """All runs in a window, newest first, optionally filtered by job."""
    init_db()
    sql = ("SELECT * FROM job_runs "
           "WHERE COALESCE(started_at, scheduled_for) BETWEEN ? AND ?")
    params: list = [since, until]
    if job_ids:
        placeholders = ",".join("?" * len(job_ids))
        sql += f" AND job_id IN ({placeholders})"
        params.extend(job_ids)
    sql += " ORDER BY COALESCE(started_at, scheduled_for) DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["detail_json"]) if d["detail_json"] else {}
        out.append(d)
    return out


def outcome_counts(since: str, until: str) -> dict[str, int]:
    """How many runs of each outcome in a window."""
    init_db()
    with connect() as conn:
        rows = conn.execute("""
            SELECT outcome, COUNT(*) AS n FROM job_runs
            WHERE COALESCE(started_at, scheduled_for) BETWEEN ? AND ?
            GROUP BY outcome
        """, (since, until)).fetchall()
    return {r["outcome"]: r["n"] for r in rows}


def unnotified_attention(outcomes: tuple[str, ...],
                         job_ids: list[str] | None = None,
                         limit: int = 25) -> list[dict]:
    """Runs that need attention and have not been alerted on yet.

    Ordered oldest-first so a burst of failures reads chronologically in
    Discord rather than backwards.

    `job_ids` narrows the query to jobs the caller considers alertable. This
    is not cosmetic: the LIMIT applies to the SQL, so without it a pile of
    runs from excluded jobs (logon-triggered tasks, disabled jobs) would
    crowd real failures out of the result entirely.
    """
    if not outcomes:
        return []
    if job_ids is not None and not job_ids:
        return []
    init_db()
    marks = ",".join("?" for _ in outcomes)
    sql = f"""
        SELECT * FROM job_runs
        WHERE outcome IN ({marks})
          AND notified_at IS NULL
    """
    args: list = list(outcomes)
    if job_ids is not None:
        sql += " AND job_id IN (" + ",".join("?" for _ in job_ids) + ")"
        args.extend(job_ids)
    sql += " ORDER BY COALESCE(started_at, scheduled_for, collected_at) ASC"
    sql += " LIMIT ?"
    args.append(limit)

    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["detail_json"]) if d["detail_json"] else {}
        out.append(d)
    return out


def mark_notified(run_keys: list[str]) -> int:
    """Stamp runs as alerted. Returns rows updated."""
    if not run_keys:
        return 0
    init_db()
    stamp = _now()
    with connect() as conn:
        n = 0
        for key in run_keys:
            cur = conn.execute(
                "UPDATE job_runs SET notified_at = ? WHERE run_key = ?",
                (stamp, key))
            n += cur.rowcount or 0
        return n


def prune(keep_days: int = 90) -> int:
    """Delete runs older than `keep_days`. Returns rows removed."""
    init_db()
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=keep_days)).isoformat()
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM job_runs WHERE COALESCE(started_at, scheduled_for) < ?",
            (cutoff,))
        return cur.rowcount or 0


# ---------------------------------------------------------------------------
# GCS sync
# ---------------------------------------------------------------------------
# WHY THIS EXISTS
# ---------------
# The collector can only run on the Windows machine: it shells out to
# `schtasks` for local tasks and to `gcloud` for cloud ones. Neither exists in
# the Cloud Run container, and `schtasks` is Windows-only by definition.
#
# So the container cannot collect. It can only READ. That means the SQLite
# file has to travel from the machine that collects to the container that
# serves, and GCS is the transport the rest of this project already uses for
# `trading_agent.db`.
#
# The collector uploads after every pass. The dashboard downloads on a TTL,
# the same way `sync_database_from_gcs` does for the trading DB.
#
# WHY A SEPARATE BLOB
# -------------------
# `jobwatch.db` is operational metadata with a different retention policy and
# a different failure mode. Keeping it in its own blob means a corrupt job
# history cannot take down trading, and vice versa.
GCS_BLOB = "jobwatch.db"


def _gcs_bucket():
    """The configured bucket, or None when GCS is not in play."""
    import os
    name = os.getenv("GCS_BUCKET_NAME")
    if not name:
        return None
    try:
        from google.cloud import storage
        return storage.Client().bucket(name)
    except Exception:
        return None


def upload_to_gcs() -> bool:
    """Push the local jobwatch DB to GCS. Returns True on success.

    Never raises: a failed upload must not fail the collection pass that
    produced the data. The local DB is still correct; only the remote view
    is stale, and the page shows the last-collected timestamp so that is
    visible.
    """
    bucket = _gcs_bucket()
    if bucket is None:
        return False
    try:
        init_db()
        # Copy to a temp file first so a concurrent write cannot produce a
        # half-uploaded blob.
        tmp = DB_PATH.with_suffix(".upload.db")
        import shutil
        shutil.copy2(DB_PATH, tmp)
        try:
            bucket.blob(GCS_BLOB).upload_from_filename(str(tmp))
        finally:
            tmp.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def download_from_gcs(max_age_s: float = 120.0) -> bool:
    """Pull the jobwatch DB from GCS if the local copy is stale.

    Returns True if a download happened. Never raises -- a missing blob or a
    network failure leaves the existing local DB in place, which is the right
    degradation: the page shows older data rather than an error.
    """
    bucket = _gcs_bucket()
    if bucket is None:
        return False

    # TTL so a page refresh does not re-download on every request.
    marker = DB_PATH.with_suffix(".last_sync")
    try:
        if marker.exists():
            age = (datetime.now(timezone.utc).timestamp()
                   - marker.stat().st_mtime)
            if age < max_age_s:
                return False
    except OSError:
        pass

    try:
        blob = bucket.blob(GCS_BLOB)
        if not blob.exists():
            return False
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = DB_PATH.with_suffix(".download.db")
        blob.download_to_filename(str(tmp))
        # Atomic replace so a reader never sees a partial file.
        tmp.replace(DB_PATH)
        marker.write_text(datetime.now(timezone.utc).isoformat(),
                          encoding="utf-8")
        return True
    except Exception:
        return False
