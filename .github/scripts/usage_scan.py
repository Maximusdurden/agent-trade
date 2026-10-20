#!/usr/bin/env python3
"""Static usage scanner for a Python codebase.

Builds a module import graph, detects dynamic/string references, identifies
entry points, and computes reachability so every file can be classified as
live / test-only / tooling / orphan.

Usage:
    python usage_scan.py <root> [--json out.json] [--pretty]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import deque
from pathlib import Path

EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "node_modules", ".idea", "site-packages",
    "dist", "build", ".eggs", ".tox", "htmlcov", ".gemini",
}
EXCLUDE_DIR_PREFIXES = ("temp_staging_",)

# Files that are conventionally executable entry points.
ENTRY_NAME_RE = re.compile(
    r"^(runner|main|app|manage|cli|mcp_server|dashboard|__main__)\.py$"
    r"|^run_.*\.py$"
    r"|^runner_.*\.py$"
    r"|^monitor_.*\.py$"
    r"|^watch_.*\.py$"
    r"|^publish_.*\.py$"
    r"|^sell_.*\.py$"
    r"|^deploy_.*\.py$"
)

# Directories whose contents are invoked directly rather than imported.
TOOLING_DIRS = {"tools", "scripts", "deploy", "bin", "migrations"}

# One-off operator scripts: run by hand, never imported.
TOOLING_NAME_RE = re.compile(
    r"^(check|verify|clean|diag|compare|analyze|analyse|inspect|dump|probe|scratch|tmp|_)\w*\.py$"
)

# A text reference only counts as an entry point when it appears in a command
# context. Bare mentions (e.g. exclusion lists in deploy scripts) do not.
COMMAND_CONTEXT_RE = re.compile(
    r"(?:\bpython3?\b|\bpy\b\s+-3|-\s*m\s|ENTRYPOINT|CMD\b|--command|--args|"
    r"Start-Process|Invoke-Expression|&\s*[\"']?[\w./\\-]*$|\.\/)",
    re.IGNORECASE,
)

DYNAMIC_PATTERNS = [
    ("importlib", re.compile(r"importlib\.(?:import_module|reload)\s*\(\s*[\"']([^\"']+)[\"']")),
    ("dunder_import", re.compile(r"__import__\s*\(\s*[\"']([^\"']+)[\"']")),
    ("py_path_literal", re.compile(r"[\"']([\w./\\-]+\.py)[\"']")),
    ("dotted_literal", re.compile(r"[\"']((?:[a-zA-Z_]\w*\.){1,}[a-zA-Z_]\w*)[\"']")),
]

TEXT_SUFFIXES = {
    ".py", ".ps1", ".psm1", ".sh", ".bat", ".cmd", ".yaml", ".yml", ".json",
    ".toml", ".cfg", ".ini", ".txt", ".md", ".dockerfile", ".tf", ".env",
}


def is_excluded(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    for part in rel.parts[:-1]:
        if part in EXCLUDE_DIRS or part.startswith(EXCLUDE_DIR_PREFIXES):
            return True
    return False


def module_name(rel: Path) -> str | None:
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    return ".".join(parts)


def package_parts(rel: Path) -> list[str]:
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    else:
        parts = parts[:-1]
    return parts


def extract_imports(tree: ast.AST, rel: Path) -> set[str]:
    """Return candidate dotted module names referenced by import statements."""
    candidates: set[str] = set()
    pkg = package_parts(rel)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                candidates.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative import: walk up (level - 1) packages from this file's package.
                base = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                prefix = ".".join(base)
                if node.module:
                    candidates.add(f"{prefix}.{node.module}" if prefix else node.module)
                else:
                    for alias in node.names:
                        candidates.add(f"{prefix}.{alias.name}" if prefix else alias.name)
            elif node.module:
                candidates.add(node.module)
                for alias in node.names:
                    candidates.add(f"{node.module}.{alias.name}")
    return candidates


def resolve(candidate: str, modules: set[str]) -> str | None:
    """Map a dotted candidate onto the longest known module prefix."""
    if candidate in modules:
        return candidate
    parts = candidate.split(".")
    for i in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in modules:
            return prefix
    return None


def scan(root: Path) -> dict:
    py_files: list[Path] = []
    text_files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or is_excluded(path, root):
            continue
        if path.suffix == ".py":
            py_files.append(path)
        elif path.suffix.lower() in TEXT_SUFFIXES or path.name.lower().startswith("dockerfile"):
            text_files.append(path)

    modules: dict[str, Path] = {}
    for path in py_files:
        name = module_name(path.relative_to(root))
        if name:
            modules[name] = path

    module_set = set(modules)
    records: dict[str, dict] = {}
    parse_errors: list[str] = []

    for path in py_files:
        rel = path.relative_to(root)
        name = module_name(rel)
        if not name:
            continue
        source = path.read_text(encoding="utf-8-sig", errors="replace")
        record = {
            "module": name,
            "path": rel.as_posix(),
            "lines": source.count("\n") + 1,
            "bytes": path.stat().st_size,
            "imports": [],
            "has_main_guard": bool(re.search(r"__name__\s*==\s*[\"']__main__[\"']", source)),
            "dynamic_refs": [],
            "is_package_init": rel.name == "__init__.py",
            "in_tooling_dir": rel.parts[0] in TOOLING_DIRS if len(rel.parts) > 1 else False,
            "is_test": rel.name.startswith("test_") or "tests" in rel.parts,
        }
        try:
            tree = ast.parse(source, filename=str(rel))
        except SyntaxError as exc:
            parse_errors.append(f"{rel.as_posix()}:{exc.lineno}: {exc.msg}")
            records[name] = record
            continue

        resolved = set()
        for candidate in extract_imports(tree, rel):
            target = resolve(candidate, module_set)
            if target and target != name:
                resolved.add(target)
        record["imports"] = sorted(resolved)

        for label, pattern in DYNAMIC_PATTERNS:
            for match in pattern.finditer(source):
                record["dynamic_refs"].append({"kind": label, "value": match.group(1)})

        records[name] = record

    # Cross-file text references (deploy scripts, Dockerfiles, YAML, docs).
    # Only references in a command context count as entry-point evidence.
    text_refs: dict[str, list[str]] = {}
    for path in text_files:
        rel = path.relative_to(root)
        if rel.suffix == ".py":
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in content.splitlines():
            if not COMMAND_CONTEXT_RE.search(line):
                continue
            for match in re.finditer(r"([\w./\\-]+\.py)", line):
                text_refs.setdefault(match.group(1).replace("\\", "/"), []).append(rel.as_posix())

    # Entry points: conventional names, main guards, or referenced by deploy text.
    referenced_paths = set(text_refs)
    entry_points: set[str] = set()
    for name, record in records.items():
        rel_path = record["path"]
        base = Path(rel_path).name
        if ENTRY_NAME_RE.match(base) or record["has_main_guard"]:
            entry_points.add(name)
        if rel_path in referenced_paths or base in referenced_paths:
            entry_points.add(name)

    # Reachability over the import graph, seeded from entry points.
    reachable: set[str] = set()
    queue = deque(sorted(entry_points))
    while queue:
        current = queue.popleft()
        if current in reachable:
            continue
        reachable.add(current)
        for dep in records.get(current, {}).get("imports", []):
            if dep not in reachable:
                queue.append(dep)

    # Dynamic references can pull in modules that static imports miss.
    dynamic_targets: set[str] = set()
    for record in records.values():
        for hit in record["dynamic_refs"]:
            value = hit["value"]
            target = resolve(value, module_set)
            if target:
                dynamic_targets.add(target)
            stem = Path(value).stem
            for name in module_set:
                if name.split(".")[-1] == stem:
                    dynamic_targets.add(name)

    for name in sorted(dynamic_targets):
        if name in reachable:
            continue
        reachable.add(name)
        queue = deque([name])
        while queue:
            current = queue.popleft()
            for dep in records.get(current, {}).get("imports", []):
                if dep not in reachable:
                    reachable.add(dep)
                    queue.append(dep)

    # Reverse dependency counts (how many modules import this one).
    imported_by: dict[str, list[str]] = {name: [] for name in records}
    for name, record in records.items():
        for dep in record["imports"]:
            imported_by.setdefault(dep, []).append(name)

    for name, record in records.items():
        record["imported_by"] = sorted(imported_by.get(name, []))
        record["imported_by_count"] = len(record["imported_by"])
        record["is_entry_point"] = name in entry_points
        record["reachable"] = name in reachable
        record["referenced_by_text"] = sorted(
            set(text_refs.get(record["path"], []) + text_refs.get(Path(record["path"]).name, []))
        )
        record["classification"] = classify(record)

    buckets: dict[str, list[str]] = {}
    for name, record in records.items():
        buckets.setdefault(record["classification"], []).append(name)

    by_class = {
        k: {
            "files": len(v),
            "lines": sum(records[n]["lines"] for n in v),
        }
        for k, v in sorted(buckets.items())
    }

    return {
        "root": str(root),
        "totals": {
            "python_files": len(records),
            "python_lines": sum(r["lines"] for r in records.values()),
            "entry_points": len(entry_points),
            "reachable": len(reachable),
            "unreachable": len(records) - len(reachable),
            "parse_errors": len(parse_errors),
            "by_classification": by_class,
        },
        "parse_errors": parse_errors,
        "entry_points": sorted(entry_points),
        "buckets": {k: sorted(v) for k, v in sorted(buckets.items())},
        "modules": records,
    }


def classify(record: dict) -> str:
    """Assign a single usage verdict to a module.

    CORE      - imported by at least one other module (library code)
    ENTRY     - executable entry point (main guard / runner name / referenced by deploy text)
    TEST      - lives under tests/ or is named test_*
    TOOLING   - one-off script under tools/ or scripts/
    ORPHAN    - no importer, no main guard, no text reference -> candidate for deletion
    """
    if record["is_test"]:
        return "TEST"
    # Package markers are structural, never dead code.
    if record["is_package_init"]:
        return "CORE"
    if record["imported_by_count"] > 0:
        return "CORE"
    # One-off operator scripts stay TOOLING even when they carry a __main__ guard.
    if record["in_tooling_dir"] or TOOLING_NAME_RE.match(Path(record["path"]).name):
        return "TOOLING"
    if record["is_entry_point"] or record["has_main_guard"]:
        return "ENTRY"
    if record["referenced_by_text"]:
        return "ENTRY"
    return "ORPHAN"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    if not args.root.is_dir():
        print(f"error: {args.root} is not a directory", file=sys.stderr)
        return 2

    result = scan(args.root.resolve())
    payload = json.dumps(result, indent=2 if args.pretty else None, sort_keys=False)
    if args.json:
        args.json.write_text(payload, encoding="utf-8")
        print(json.dumps(result["totals"], indent=2))
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
