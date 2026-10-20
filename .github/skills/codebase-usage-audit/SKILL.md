---
name: codebase-usage-audit
description: Deep usage audit of a large Python codebase: crawl every file, classify it as used / test-only / tooling / orphan, and write a recall-friendly inventory report.
disable-model-invocation: true
argument-hint: "Root path to audit (default: this repository root)"
---
# Codebase Usage Audit

Perform a **deep, evidence-based usage audit** of the Python codebase at
`${input:root:Z:\python\projects\agent-trade}`.

The goal is a **recall-friendly inventory**: a reader should be able to open one
report and immediately know which files matter, which are dead weight, and where
a clean rewrite should start. Optimize for *clarity and recall*, not prose.

## Non-negotiable rules

1. **Evidence over intuition.** Every verdict must trace to a concrete signal:
   an import statement, a `__main__` guard, a reference in a deploy script /
   Dockerfile / YAML / docs, or a dynamic string reference. Never guess.
2. **Never modify source code.** This is a read-only audit. The only files you
   may write are the report and the machine-readable JSON.
3. **Never delete or move anything.** Recommend, do not act.
4. **Report uncertainty explicitly.** If a file's status cannot be proven, mark
   it `UNCERTAIN` with the reason. A wrong "unused" verdict is worse than an
   honest "unknown".
5. **Exclude noise.** Skip `.venv/`, `venv/`, `__pycache__/`, `.git/`,
   `.pytest_cache/`, `node_modules/`, and `temp_staging_*/` directories.

## Procedure

### Step 1 — Mechanical scan (do this first, always)

Run the bundled scanner. It builds the import graph, resolves relative imports,
detects dynamic references, finds entry points, and computes reachability.

```powershell
python .github/scripts/usage_scan.py . --json .github/reports/usage-scan.json --pretty
```

Read the JSON. It contains, per module: `path`, `lines`, `imports`,
`imported_by`, `imported_by_count`, `has_main_guard`, `is_entry_point`,
`reachable`, `referenced_by_text`, `dynamic_refs`, and `classification`.

### Step 2 — Verify the mechanical result

The scanner is a starting point, not the answer. Independently confirm:

- **Spot-check 5+ `ORPHAN` verdicts** by grepping the whole repo (including
  non-Python files) for the module name and its filename stem. A file can be
  live via a string reference, a subprocess call, a scheduler config, or a
  Cloud Run job definition.
- **Spot-check 5+ `CORE` verdicts** by confirming the importer actually uses the
  symbol, not just that the import line exists.
- **Check `dynamic_refs`** for `importlib`, `__import__`, and `.py` string
  literals — these can make an "unreachable" file reachable.
- **Check deploy/scheduler configs** (`deploy/*.ps1`, `cloudbuild*.yaml`,
  `Dockerfile*`, `.github/workflows/`) for entry points the scanner missed.

### Step 3 — Classify every file

Assign exactly one verdict per file:

| Verdict | Meaning | Evidence required |
|---------|---------|-------------------|
| `CORE` | Library code imported by other modules | ≥1 real importer |
| `ENTRY` | Executable entry point (runner, monitor, deploy target) | `__main__` guard, runner-style name, or referenced by deploy/config text |
| `TEST` | Test suite | Under `tests/` or named `test_*` |
| `TOOLING` | One-off operator script under `tools/` | Lives in a tooling dir, no importers |
| `ORPHAN` | No importer, no entry point, no text reference | All signals negative |
| `UNCERTAIN` | Cannot be proven either way | State the specific blocker |

### Step 4 — Write the report

Write to `.github/reports/codebase-usage-audit.md`. Use this structure:

```markdown
# Codebase Usage Audit — <repo name>

**Audited:** <date> · **Root:** <path>
**Scope:** <N> Python files · <N> lines · <N> non-Python config/doc files

## 1. Executive Summary
- 3-6 bullets: what this codebase actually is, what runs in production,
  what is dead, and the single biggest cleanup opportunity.

## 2. Verdict Counts
| Verdict | Files | Lines | % of lines |
|---|---|---|---|

## 3. The Live Core (read this first)
The modules that everything depends on, ranked by importer count.
| Module | Path | Imported by | Lines | One-line purpose |

## 4. Entry Points (what actually runs)
Grouped by lane (e.g. main runner, options, swing, blog, dashboard, monitors).
| Entry point | Path | Triggered by | Purpose |

## 5. Orphans — Safe to Delete (candidates)
| Path | Lines | Why it looks dead | Confidence | Verify before deleting |
Only list files where every signal is negative. Include the exact grep command
a human should run to double-check.

## 6. Uncertain — Needs a Human Decision
| Path | Lines | Blocker | What would resolve it |

## 7. Duplicate & Overlapping Clusters
Groups of files that appear to do the same job (e.g. many `backtest_*.py`,
`compare_*.py`, `check_*.py`). Name the cluster, list members, and
recommend the one to keep.

## 8. Dependency Hotspots
Modules with high fan-in that a rewrite must preserve or deliberately replace.

## 9. Clean-Rewrite Starting Point
- **Keep as-is:** <list>
- **Port with cleanup:** <list>
- **Drop:** <list>
- **Consolidate:** <cluster> → <target>

## 10. Method & Limitations
Exact commands run, scanner version, what static analysis cannot see
(dynamic imports, subprocess calls, external schedulers, runtime config).
```

### Step 5 — Recall aids

The report must be scannable months later:

- Put the **verdict in the first column** of every table.
- Use the **exact repo-relative path** for every file, never a bare filename.
- Keep every "purpose" cell to **one line, under 100 characters**.
- Add a **one-line TL;DR at the top of each section**.
- Prefer tables over paragraphs everywhere.

## Output

Return a short chat summary (≤15 lines) containing:
1. Verdict counts.
2. The top 5 live core modules.
3. The orphan list (paths only).
4. The single recommended starting point for the rewrite.
5. Absolute paths to the report and the JSON.
