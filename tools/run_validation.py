#!/usr/bin/env python3
"""Validation runner for agent-trade (TMCL-747 Validation Framework).

Runs the full Testing & Validation Protocol from sprint_plan.md:

    1. Static code analysis (ruff/flake8) — optional, if available.
    2. Mock-mode dry-run of the runner — optional (--dry-run).
    3. Unit regression suite (pytest tests/) — optional (--tests).
    4. Audit DB integrity (tools/validate_db.py).

By default it runs the DB integrity validation. Use flags to enable the
optional heavier checks.

Usage:
    python tools/run_validation.py                 # DB integrity only
    python tools/run_validation.py --tests         # + pytest suite
    python tools/run_validation.py --lint          # + ruff/flake8
    python tools/run_validation.py --dry-run       # + mock runner dry-run
    python tools/run_validation.py --all           # everything
"""
import argparse
import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(cmd: list[str], cwd: str = PROJECT_ROOT) -> tuple[int, str]:
    """Run a command and return (exit_code, output)."""
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        output = proc.stdout + proc.stderr
        return proc.returncode, output
    except FileNotFoundError:
        return 127, f"Command not found: {cmd[0]}"


def main():
    parser = argparse.ArgumentParser(description="Run agent-trade validation protocol.")
    parser.add_argument("--tests", action="store_true", help="Run pytest regression suite")
    parser.add_argument("--lint", action="store_true", help="Run ruff/flake8 static analysis")
    parser.add_argument("--dry-run", action="store_true", help="Run mock-mode runner dry-run")
    parser.add_argument("--all", action="store_true", help="Run all checks")
    parser.add_argument("--db", default="trading_agent.db", help="DB path for integrity check")
    args = parser.parse_args()

    all_checks = args.all
    results = []

    # 1. Static code analysis
    if args.lint or all_checks:
        print("=== Static Code Analysis ===")
        for tool in (["ruff", "check", "."], ["flake8", "."]):
            code, out = run(tool)
            status = "PASS" if code == 0 else "FAIL"
            results.append((status, f"{tool[0]}"))
            print(f"[{status}] {tool[0]}")
            if out.strip():
                print(out.strip()[-2000:])
        print()

    # 2. Mock-mode dry-run
    if args.dry_run or all_checks:
        print("=== Mock-Mode Dry-Run ===")
        code, out = run([sys.executable, "runner.py", "--once", "--dry-run"])
        status = "PASS" if code == 0 else "FAIL"
        results.append((status, "runner dry-run"))
        print(f"[{status}] runner.py --once --dry-run")
        if out.strip():
            print(out.strip()[-2000:])
        print()

    # 3. Unit regression suite
    if args.tests or all_checks:
        print("=== Unit Regression Suite ===")
        code, out = run([sys.executable, "-m", "pytest", "tests/", "-q"])
        status = "PASS" if code == 0 else "FAIL"
        results.append((status, "pytest"))
        print(f"[{status}] pytest tests/")
        if out.strip():
            print(out.strip()[-2000:])
        print()

    # 4. Audit DB integrity
    print("=== Audit DB Integrity ===")
    code, out = run([sys.executable, "tools/validate_db.py", args.db])
    status = "PASS" if code == 0 else "FAIL"
    results.append((status, "db integrity"))
    print(f"[{status}] validate_db {args.db}")
    print(out.strip())
    print()

    # Summary
    print("=" * 50)
    print("VALIDATION SUMMARY")
    for status, name in results:
        print(f"  [{status}] {name}")
    failed = [name for status, name in results if status == "FAIL"]
    if failed:
        print(f"\nFAILED: {', '.join(failed)}")
        return 1
    print("\nALL VALIDATION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())