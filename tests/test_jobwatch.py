"""Tests for JobWatch.

Same conventions as the rest of the suite: plain asserts, a `main()` that
prints a summary, exit 0 on pass and 1 on failure.

WHAT IS TESTED, AND WHY
-----------------------
JobWatch exists to make silent failures visible. The ways it can fail are
specific:

    outcome logic      a run that exits 0 and does nothing must be `no_op`,
                       not `ok` -- that is the whole point
    rule evaluation    a rule that cannot be evaluated must be `unknown`,
                       never `ok` and never `failed`
    idempotency        the collector runs hourly over overlapping windows;
                       re-collecting must update, not duplicate
    redaction          log excerpts are shown in a browser; a traceback that
                       prints an API key must not leak it
    filters            a filter that silently matches nothing looks like a
                       quiet day
    timezone           everything is UTC in storage; a job at 20:00 New York
                       must not land on the wrong day
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobwatch import api, collect, registry, store   # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


# ---------------------------------------------------------------- registry
def test_registry() -> None:
    section("registry")

    problems = registry.validate()
    check("registry validates", not problems, "; ".join(problems))

    ids = [j.job_id for j in registry.ALL_JOBS]
    check("job ids unique", len(set(ids)) == len(ids))

    check("has local jobs", len(registry.LOCAL_JOBS) > 0)
    check("has cloud jobs", len(registry.CLOUD_JOBS) > 0)

    for j in registry.ALL_JOBS:
        check(f"{j.job_id} source valid", j.source in registry.SOURCES, j.source)
        check(f"{j.job_id} tz has slash", "/" in j.timezone, j.timezone)
        check(f"{j.job_id} category set", bool(j.category))

    # every local job must carry the exact task name, or the collector
    # silently matches nothing
    for j in registry.LOCAL_JOBS:
        check(f"{j.job_id} has task_name", bool(j.task_name), "missing")

    # every cloud job must name the Cloud Run job it triggers
    for j in registry.CLOUD_JOBS:
        check(f"{j.job_id} has target", bool(j.target), "missing")

    try:
        registry.get("nope")
        check("unknown job raises", False, "no exception")
    except KeyError:
        check("unknown job raises", True)


# ------------------------------------------------------------ rule engine
def test_rules() -> None:
    section("rule evaluation")

    job = registry.Job("t", "T", "local", "daily", "UTC", True, "trading",
                       success_rule={"markets": (">", 0)})

    ok, why = registry.evaluate_rule(job, {"markets": 631})
    check("rule passes on good value", ok is True, why)

    ok, why = registry.evaluate_rule(job, {"markets": 0})
    check("rule fails on zero", ok is False, why)

    # A missing field must be None, NOT False. "We could not tell" must not
    # be reported as "it failed", and must not be reported as "it worked".
    ok, why = registry.evaluate_rule(job, {})
    check("missing field is None", ok is None, why)

    # No rule at all is also None, not True.
    norule = registry.Job("t2", "T2", "local", "daily", "UTC", True, "trading")
    ok, why = registry.evaluate_rule(norule, {"markets": 5})
    check("no rule is None", ok is None, why)

    # every operator
    for op, val, expect in ((">", 6, True), (">=", 5, True), ("<", 4, True),
                            ("<=", 5, True), ("==", 5, True), ("!=", 6, True),
                            ("in", 5, True), ("not_in", 7, True)):
        j = registry.Job("t", "T", "local", "d", "UTC", True, "trading",
                         success_rule={"x": (op, (5, 6) if op in
                                             ("in", "not_in") else 5)})
        got, why = registry.evaluate_rule(j, {"x": val})
        check(f"operator {op}", got is expect, f"{got} vs {expect}: {why}")

    # a type mismatch must be None, not a crash
    j = registry.Job("t", "T", "local", "d", "UTC", True, "trading",
                     success_rule={"x": (">", 0)})
    got, why = registry.evaluate_rule(j, {"x": "not a number"})
    check("type mismatch is None", got is None, why)


# --------------------------------------------------------------- redaction
def test_redaction() -> None:
    section("redaction")

    cases = [
        ("api_key=sk-abcdefghijklmnop1234", "sk-abcdefghijklmnop1234"),
        ("token: ghp_abcdefghijklmnopqrstuvwxyz", "ghp_abcdefghijklmnopqrstuvwxyz"),
        ("AWS AKIAIOSFODNN7EXAMPLE here", "AKIAIOSFODNN7EXAMPLE"),
        ("password=hunter2", "hunter2"),
        ("-----BEGIN RSA PRIVATE KEY-----", "BEGIN RSA PRIVATE KEY"),
    ]
    for text, secret in cases:
        out = collect.redact(text)
        check(f"redacts {secret[:12]}", secret not in out, out)

    # ordinary text must survive
    plain = "2026-11-04 10:00:01 INFO markets=631 signals=0"
    check("plain text unchanged", collect.redact(plain) == plain,
          collect.redact(plain))

    check("empty is empty", collect.redact("") == "")


# ------------------------------------------------------------------ store
def test_store() -> None:
    section("store")

    # Use a scratch DB so the test cannot touch real history.
    import jobwatch.store as store_mod
    scratch = ROOT / "data" / "_test_jobwatch.db"
    saved = store_mod.DB_PATH
    store_mod.DB_PATH = scratch
    try:
        for suffix in ("", "-wal", "-shm"):
            Path(str(scratch) + suffix).unlink(missing_ok=True)
        store.init_db()

        row = {
            "job_id": "local:nba-prop-daily", "source": "local",
            "started_at": "2026-11-04T15:00:00+00:00",
            "scheduled_for": "2026-11-04T15:00:00+00:00",
            "outcome": "ok", "did_work": True,
            "detail": {"markets": 631}, "source_ref": "NBA Prop Daily",
        }
        store.upsert_run(row)
        latest = store.latest_per_job()
        check("row stored", "local:nba-prop-daily" in latest)
        check("detail round-trips",
              latest["local:nba-prop-daily"]["detail"].get("markets") == 631)

        # IDEMPOTENCY: the collector re-reads overlapping windows every hour.
        # Re-collecting the same run must update, not duplicate.
        store.upsert_run(row)
        store.upsert_run(row)
        runs = store.runs_for("local:nba-prop-daily")
        check("re-collect does not duplicate", len(runs) == 1, str(len(runs)))

        # an update must overwrite, not append
        row2 = dict(row, outcome="no_op", did_work=False,
                    detail={"markets": 0})
        store.upsert_run(row2)
        runs = store.runs_for("local:nba-prop-daily")
        check("update overwrites", len(runs) == 1, str(len(runs)))
        check("update changes outcome", runs[0]["outcome"] == "no_op",
              runs[0]["outcome"])

        # a row with NO timestamp must still appear in latest_per_job.
        # The first implementation used a MAX() self-join and silently
        # dropped every row with a NULL timestamp -- 27 of 32 jobs vanished.
        store.upsert_run({
            "job_id": "local:disabled-thing", "source": "local",
            "outcome": "skipped", "did_work": None,
            "detail": {"reason": "disabled"}, "source_ref": "x",
        })
        latest = store.latest_per_job()
        check("null-timestamp row appears",
              "local:disabled-thing" in latest, str(list(latest)))

        # window queries
        rows = store.runs_between("2026-11-01T00:00:00+00:00",
                                  "2026-11-05T00:00:00+00:00")
        check("window query finds rows", len(rows) >= 1, str(len(rows)))
        rows = store.runs_between("2020-01-01T00:00:00+00:00",
                                  "2020-01-02T00:00:00+00:00")
        check("empty window returns nothing", rows == [], str(len(rows)))

        counts = store.outcome_counts("2026-11-01T00:00:00+00:00",
                                      "2026-11-05T00:00:00+00:00")
        check("outcome counts work", counts.get("no_op") == 1, str(counts))
    finally:
        store_mod.DB_PATH = saved
        # WAL mode leaves -wal and -shm sidecars open; deleting only the main
        # file raises PermissionError on Windows.
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(scratch) + suffix).unlink(missing_ok=True)
            except PermissionError:
                pass


# -------------------------------------------------------------------- api
def test_api() -> None:
    section("api")

    code, payload = api.dispatch("/api/jobs", {})
    check("jobs returns 200", code == 200, str(code))
    check("jobs has all jobs",
          payload.get("total_jobs") == len(registry.ALL_JOBS),
          str(payload.get("total_jobs")))
    check("jobs has counts", isinstance(payload.get("counts"), dict))
    check("jobs has attention list", isinstance(payload.get("attention"), list))

    # The main payload must NOT carry log excerpts. Sending 32 of them made
    # it 717 KB; the detail endpoint exists for that.
    jobs = payload.get("jobs") or []
    with_log = [j for j in jobs if j.get("log_excerpt")]
    check("main payload omits log excerpts", not with_log,
          f"{len(with_log)} jobs carried a log")

    # filters
    code, p = api.dispatch("/api/jobs", {"q": "swing"})
    check("search filter narrows", p["shown"] < p["total_jobs"],
          f"{p['shown']} of {p['total_jobs']}")

    code, p = api.dispatch("/api/jobs", {"include_disabled": "0"})
    check("hide-disabled narrows", p["shown"] < p["total_jobs"],
          f"{p['shown']} of {p['total_jobs']}")
    check("hide-disabled removes all disabled",
          all(j["enabled"] for j in p["jobs"]), "a disabled job survived")

    code, p = api.dispatch("/api/jobs", {"category": "trading"})
    check("category filter works",
          all(j["category"] == "trading" for j in p["jobs"]), "wrong category")

    code, p = api.dispatch("/api/jobs", {"source": "scheduler"})
    check("source filter works",
          all(j["source"] == "scheduler" for j in p["jobs"]), "wrong source")

    # unknown path
    code, p = api.dispatch("/api/nope", {})
    check("unknown path is 404", code == 404, str(code))

    # detail endpoint
    code, p = api.dispatch("/api/jobs/detail",
                           {"job_id": "local:nba-prop-daily"})
    check("detail returns 200", code == 200, str(code))
    check("detail has history", isinstance(p.get("history"), list))

    code, p = api.dispatch("/api/jobs/detail", {"job_id": "nope"})
    check("detail unknown job errors", "error" in p, str(p))

    # history endpoint
    code, p = api.dispatch("/api/jobs/history", {})
    check("history returns 200", code == 200, str(code))
    check("history has since/until", "since" in p and "until" in p)


# ------------------------------------------------------------------ notify
def test_notify() -> None:
    """The failure alert must fire once per run, and never on no_op.

    The failure mode this guards against is the one that kills alerting:
    paging on every hourly pass for the same failure until the channel is
    muted.
    """
    section("notify")

    from jobwatch import notify

    # no_op is deliberately excluded: it means "ran, did nothing", which is
    # worth showing on the page but not worth waking someone up.
    check("no_op is not an alert outcome",
          registry.NO_OP not in notify.ALERT_OUTCOMES,
          str(notify.ALERT_OUTCOMES))
    check("failed is an alert outcome",
          registry.FAILED in notify.ALERT_OUTCOMES)
    check("missed is an alert outcome",
          registry.MISSED in notify.ALERT_OUTCOMES)

    # A logon-triggered task must never alert. `Master MCP Gateway` reports
    # exit 0xC000013A (STATUS_CONTROL_C_EXIT) on every shutdown, which is the
    # process being killed at logoff, not a failure. Alerting on it would
    # page on every reboot.
    alertable = notify.alertable_job_ids()
    check("logon-triggered jobs are not alertable",
          "local:master-mcp-gateway" not in alertable,
          "master-mcp-gateway would alert on every shutdown")
    check("alertable excludes unscheduled jobs",
          all(registry.BY_ID[i].scheduled for i in alertable),
          "an unscheduled job is alertable")
    check("alertable excludes disabled jobs",
          all(registry.BY_ID[i].enabled for i in alertable),
          "a disabled job is alertable")
    check("alertable is not empty", len(alertable) > 0, str(len(alertable)))

    # message composition
    run = {
        "run_key": "k1", "job_id": "local:nba-prop-daily",
        "outcome": "failed", "started_at": "2026-11-04T15:00:00+00:00",
        "exit_code": 1, "detail": {"rule": "markets=0 fails > 0"},
    }
    msg = notify.build_message([run])
    check("message names the job", "NBA Prop Daily" in msg, msg)
    check("message includes the rule reason", "markets=0" in msg, msg)
    check("message includes exit code", "exit 1" in msg, msg)
    check("singular failure is not pluralised",
          "1 job failure" in msg and "failures" not in msg, msg)

    two = notify.build_message([run, dict(run, run_key="k2")])
    check("plural failure is pluralised", "2 job failures" in two, two)

    # Discord hard-caps content at 2000 characters.
    many = [dict(run, run_key=f"k{i}") for i in range(40)]
    big = notify.build_message(many)
    check("long message is truncated under 2000", len(big) <= 2000,
          str(len(big)))
    check("long message summarises the remainder", "more" in big, big[-120:])

    # dedupe: a run is alerted once, then stamped
    saved = store.DB_PATH
    scratch = Path(__file__).resolve().parent / "_scratch_notify.db"
    try:
        for suffix in ("", "-wal", "-shm"):
            Path(str(scratch) + suffix).unlink(missing_ok=True)
        store.DB_PATH = scratch
        store.init_db()

        store.upsert_run({
            "job_id": "local:nba-prop-daily", "source": "local",
            "started_at": "2026-11-04T15:00:00+00:00",
            "scheduled_for": "2026-11-04T15:00:00+00:00",
            "outcome": "failed", "did_work": False,
            "detail": {"rule": "markets=0 fails > 0"}, "source_ref": "t1",
        })
        store.upsert_run({
            "job_id": "local:webmaster-agent", "source": "local",
            "started_at": "2026-11-04T15:05:00+00:00",
            "scheduled_for": "2026-11-04T15:05:00+00:00",
            "outcome": "no_op", "did_work": False,
            "detail": {"rule": "markets=0 fails > 0"}, "source_ref": "t2",
        })

        pending = store.unnotified_attention(notify.ALERT_OUTCOMES)
        check("only the failure is pending", len(pending) == 1,
              str([r["job_id"] for r in pending]))
        check("pending excludes no_op",
              all(r["outcome"] != "no_op" for r in pending),
              str([r["outcome"] for r in pending]))

        # dry run must not stamp
        dry = notify.notify_failures(dry_run=True)
        check("dry run reports the count", dry.get("count") == 1, str(dry))
        check("dry run does not stamp",
              len(store.unnotified_attention(notify.ALERT_OUTCOMES)) == 1,
              "dry run consumed the alert")

        # stamping clears it
        store.mark_notified([r["run_key"] for r in pending])
        check("stamping clears the pending alert",
              store.unnotified_attention(notify.ALERT_OUTCOMES) == [],
              "still pending after stamp")

        # re-collecting the same run must not resurrect the alert
        store.upsert_run({
            "job_id": "local:nba-prop-daily", "source": "local",
            "started_at": "2026-11-04T15:00:00+00:00",
            "scheduled_for": "2026-11-04T15:00:00+00:00",
            "outcome": "failed", "did_work": False,
            "detail": {"rule": "markets=0 fails > 0"}, "source_ref": "t1",
        })
        check("re-collect does not resurrect the alert",
              store.unnotified_attention(notify.ALERT_OUTCOMES) == [],
              "upsert reset notified_at")
    finally:
        store.DB_PATH = saved
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(scratch) + suffix).unlink(missing_ok=True)
            except PermissionError:
                pass


# --------------------------------------------------------------- timezone
def test_timezone() -> None:
    section("timezone handling")

    # A date-only string must be interpreted as UTC midnight, not local.
    got = api._parse_when("2026-11-04", datetime.now(timezone.utc))
    check("date-only parses as UTC", got.startswith("2026-11-04T00:00:00"),
          got)

    # A full ISO timestamp with an offset must be converted to UTC.
    got = api._parse_when("2026-11-04T20:00:00-05:00",
                          datetime.now(timezone.utc))
    check("offset converted to UTC", got.startswith("2026-11-05T01:00:00"),
          got)

    # Garbage must fall back to the default, not raise.
    default = datetime(2026, 1, 1, tzinfo=timezone.utc)
    got = api._parse_when("not a date", default)
    check("garbage falls back", got.startswith("2026-01-01"), got)

    # None must fall back too.
    got = api._parse_when(None, default)
    check("none falls back", got.startswith("2026-01-01"), got)


# ------------------------------------------------------------- outcome map
def test_outcome_semantics() -> None:
    section("outcome semantics")

    # The five outcomes must be distinct and cover the states that matter.
    for o in (registry.OK, registry.NO_OP, registry.FAILED,
              registry.MISSED, registry.SKIPPED):
        check(f"{o} defined", o in registry.OUTCOMES, o)

    # no_op and missed are the two that no single source can report, and they
    # are the two that matter. They must be in the attention set.
    check("no_op needs attention", registry.NO_OP in registry.ATTENTION)
    check("missed needs attention", registry.MISSED in registry.ATTENTION)
    check("failed needs attention", registry.FAILED in registry.ATTENTION)

    # ok and skipped must NOT be in the attention set, or the page cries wolf.
    check("ok is not attention", registry.OK not in registry.ATTENTION)
    check("skipped is not attention",
          registry.SKIPPED not in registry.ATTENTION)


# -------------------------------------------------------------------- main
def main() -> int:
    print("=" * 60)
    print("JOBWATCH TESTS")
    print("=" * 60)

    test_registry()
    test_rules()
    test_redaction()
    test_store()
    test_api()
    test_notify()
    test_timezone()
    test_outcome_semantics()

    print()
    print("=" * 60)
    print(f"PASS {PASS}   FAIL {FAIL}")
    if FAILURES:
        print()
        for f in FAILURES:
            print(f"  - {f}")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
