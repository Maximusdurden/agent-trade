#!/usr/bin/env python3
"""What-the-agent-is-learning report for agent-trade.

Pulls the latest ``strategy_history`` row per ticker (rule + meta_reasoning +
authoring model) and combines it with current decayed performance to produce a
readable digest of the agent's evolving thesis on each name.

Output: reports/ticker_learning_report.md

Usage:
  python tools/ticker_learning_report.py
"""
import argparse
import logging
import os
import sqlite3
import sys
from collections import defaultdict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)

from core import config  # noqa: E402
from core.feedback import compute_closed_round_trips, symbol_stats  # noqa: E402
from core.strategy_rules import is_crypto_symbol  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("TickerLearning")

REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)


def latest_strategy_rows(db_path: str) -> dict:
    """Return {ticker: {todays_rules, meta_reasoning, strategy_version, timestamp}}
    for the latest strategy_history row per ticker."""
    if not os.path.exists(db_path):
        logger.warning(f"DB not found: {db_path}")
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT ticker, todays_rules, meta_reasoning, strategy_version, timestamp
        FROM strategy_history
        WHERE id IN (
            SELECT MAX(id) FROM strategy_history GROUP BY ticker
        )
    """).fetchall()
    conn.close()
    out = {}
    for r in rows:
        out[r["ticker"]] = {
            "todays_rules": r["todays_rules"] or "",
            "meta_reasoning": r["meta_reasoning"] or "",
            "strategy_version": r["strategy_version"] or "",
            "timestamp": r["timestamp"] or "",
        }
    return out


def compute_stats() -> dict:
    trips = compute_closed_round_trips()
    by_symbol = defaultdict(list)
    for t in trips:
        by_symbol[t["symbol"]].append(t)
    out = {}
    for sym, sym_trips in by_symbol.items():
        st = symbol_stats(sym)
        st["total_pnl"] = sum(t["pnl"] for t in sym_trips)
        out[sym] = st
    return out


def _model_from_version(version: str) -> str:
    """Extract the authoring model from a strategy_version tag like
    'v20260911-123347|model=deepseek-deepseek-r1'."""
    for part in version.split("|"):
        if part.startswith("model="):
            return part.split("=", 1)[1]
    return "unknown"


def build_report(strategy: dict, stats: dict) -> str:
    # Group tickers by asset class.
    equities, cryptos, options = [], [], []
    for sym in strategy:
        up = sym.upper()
        if up.startswith("OPTIONS/") or is_option_contract(up):
            options.append(sym)
        elif is_crypto_symbol(sym):
            cryptos.append(sym)
        else:
            equities.append(sym)

    def sort_key(s):
        st = stats.get(s, {})
        return st.get("total_pnl", 0.0)

    equities.sort(key=sort_key, reverse=True)
    cryptos.sort(key=sort_key, reverse=True)
    options.sort(key=sort_key, reverse=True)

    lines = []
    lines.append("# Agent-Trade: What the Agent Is Learning (per ticker)")
    lines.append("")
    lines.append(f"**Generated:** {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M %Z')}")
    lines.append("")
    lines.append("Source: latest `strategy_history` row per ticker + decayed performance from the cloud DB.")
    lines.append("")

    for title, group in (("## Equities", equities), ("## Crypto", cryptos), ("## Options", options)):
        if not group:
            continue
        lines.append(title)
        lines.append("")
        for sym in group:
            st = stats.get(sym, {})
            sr = strategy[sym]
            n = st.get("n_trades", 0)
            wr = st.get("win_rate", 0.0)
            pnl = st.get("total_pnl", 0.0)
            model = _model_from_version(sr["strategy_version"])
            lines.append(f"### {sym}")
            lines.append("")
            lines.append(f"- **Performance:** {n} closed RTs | WR {wr:.1f}% | PnL ${pnl:+,.2f}")
            lines.append(f"- **Authoring model:** `{model}`")
            lines.append(f"- **Latest rule:** {sr['todays_rules']}")
            if sr["meta_reasoning"]:
                lines.append(f"- **Meta-reasoning:** {sr['meta_reasoning']}")
            lines.append("")

    return "\n".join(lines)


def is_option_contract(sym: str) -> bool:
    """Rough OCC contract check (6-char root + 6-digit date + C/P + 8-digit strike)."""
    import re
    return bool(re.match(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$", sym))


def main():
    parser = argparse.ArgumentParser(description="Generate the ticker learning report")
    parser.add_argument("--db", default=str(config.DATABASE_PATH),
                        help="Path to the DB (default: config.DATABASE_PATH)")
    parser.add_argument("--out", default=os.path.join(REPORTS_DIR, "ticker_learning_report.md"),
                        help="Output markdown path")
    args = parser.parse_args()

    strategy = latest_strategy_rows(args.db)
    if not strategy:
        logger.error("No strategy_history rows found. Is the DB pulled from GCS?")
        return 1
    stats = compute_stats()
    report = build_report(strategy, stats)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    logger.info(f"Wrote learning report to {args.out} ({len(strategy)} tickers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())