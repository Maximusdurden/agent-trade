"""JobWatch -- a single view of every scheduled job, local and cloud.

WHY THIS EXISTS
---------------
Jobs are spread across three systems that do not know about each other:

    Windows Task Scheduler   8 enabled tasks, 7 disabled
    Cloud Scheduler          17 jobs in us-central1
    Cloud Run jobs           13 batch jobs

Answering "did everything run today?" meant checking all three by hand, and
the three disagree about what "ok" means. Task Scheduler reports `result=0`
for a run that produced nothing. Cloud Scheduler reports `ENABLED` for a job
whose target has been failing for a week. Neither knows whether the work
actually happened.

THE THREE STATES THAT MATTER
----------------------------
A job run is one of:

    ok        ran, exited 0, and did something
    no_op     ran, exited 0, and did nothing        <- the silent failure
    failed    ran and exited non-zero
    missed    was scheduled and never ran           <- laptop asleep
    skipped   disabled or paused, so not expected

`no_op` and `missed` are the two states no single source can tell you, and
they are the two that matter. A job that quietly does nothing looks exactly
like a healthy job in every dashboard that exists today.

DESIGN
------
    registry.py    the list of jobs, and the per-job rule for "did it work"
    store.py       SQLite: job_runs, one row per observed run
    collect.py     the collector -- reads all three sources, writes rows
    api.py         the JSON the dashboard serves
    page.py        the HTML page

The collector is the only thing that writes. The API only reads. That split
means a slow or broken collector cannot make the page hang, and the page
cannot corrupt the history.
"""
