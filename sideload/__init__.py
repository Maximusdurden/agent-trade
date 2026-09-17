"""Sideloaded experimental trading lane.

A dedicated, paper-trading lane that owns AMD exclusively and learns AMD daily
on its own. It reuses agent-trade's indicator engine, brain, guardrails, and
options stack, but with AMD-tuned rules. It writes to the SAME database so AMD
flows automatically to both treatmotivated.capital (blog) and
dashboard.agenttrade.us (decisions/thoughts) — zero changes to either.

Modules:
    config_sideload   — AMD-tuned config overrides (env-driven, isolated).
    runner_sideload   — AMD-only trading cycle (paper, same DB).
    backtest_amd      — deterministic grid-search backtest (grid-only, no ML).
    learn_amd         — daily learning agent (edge discovery -> tuned rule).
    publish_fact_of_day — "Dexter's AMD Fact of the Day" blog post.
"""