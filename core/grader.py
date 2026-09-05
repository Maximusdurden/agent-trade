#!/usr/bin/env python3
"""D-TAG: Dexter's Trade Analyzer & Grader (ported to agent-trade).

Ports dexter's ``utilities/trade_grader.py`` into ``core/grader.py``. Reads the
blog ``realized_trades`` mirror (built by ``tools/build_blog_db``) and writes
grades into a ``realized_trade_grades`` table in the same DB — so the blog posts
carry the grade badge, exactly as before.

Changes from the original:
  - Takes an explicit ``db_path`` (works against the GCS-pulled DB in the cloud).
  - DB helpers are self-contained here (create ``realized_trade_grades`` if needed).
  - Uses agent-trade's ``core.feedback.is_option_contract_symbol`` / ``option_underlying``.
  - Market data via yfinance (fallback to Alpaca) — needs egress in the cloud.

Usage:
    python -m core.grader --date YYYY-MM-DD [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

import pandas as pd
import pytz

logger = logging.getLogger("DexterGrader")

# yfinance is used for market data in grading. Import lazily so the grader can be
# imported (and gracefully skip grading) even when yfinance isn't installed yet
# (e.g. local dev); it is a hard dependency in the cloud image.
try:
    import yfinance as yf
    yf  # noqa: B018  (ensure referenced)
except ImportError:  # pragma: no cover
    yf = None

from core.config import DATABASE_PATH

# Sector benchmark map (same as the original).
SECTOR_MAP = {
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "AVGO": "XLK", "AMD": "XLK",
    "GOOGL": "XLC", "GOOG": "XLC", "META": "XLC", "NFLX": "XLC", "DIS": "XLC",
    "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY", "NKE": "XLY", "SBUX": "XLY",
    "JPM": "XLF", "BAC": "XLF", "MS": "XLF", "GS": "XLF", "V": "XLF", "MA": "XLF",
    "LLY": "XLV", "UNH": "XLV", "JNJ": "XLV", "ABBV": "XLV", "MRK": "XLV",
    "XOM": "XLE", "CVX": "XLE", "SLB": "XLE",
    "CAT": "XLI", "GE": "XLI", "HON": "XLI", "LMT": "XLI",
    "LIN": "XLB", "FCX": "XLB", "NEM": "XLB",
    "NEE": "XLU", "DUK": "XLU", "SO": "XLU",
    "PLD": "XLRE", "AMT": "XLRE",
    "PG": "XLP", "KO": "XLP", "PEP": "XLP", "COST": "XLP",
    "BTC/USD": "BTC/USD", "ETH/USD": "BTC/USD",
}

NY_TZ = pytz.timezone("America/New_York")


# ---------------------------------------------------------------------------
# DB helpers (self-contained, explicit db_path)
# ---------------------------------------------------------------------------
def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS realized_trade_grades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER,
            ticker TEXT,
            date TEXT,
            stock_return REAL, spy_return REAL, sector_return REAL, sector_symbol TEXT,
            alpha_vs_spy REAL, alpha_vs_sector REAL,
            mfe REAL, mae REAL, capture_ratio REAL,
            alpha_score REAL, risk_score REAL, execution_score REAL, composite_score REAL,
            grade TEXT,
            UNIQUE(trade_id)
        )
    """)


def _load_realized_trades(db_path: str) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        return pd.read_sql("SELECT * FROM realized_trades", conn)
    finally:
        conn.close()


def _save_grade(db_path: str, g: dict) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        _ensure_schema(conn)
        conn.execute("""
            INSERT INTO realized_trade_grades (
                trade_id, ticker, date, stock_return, spy_return, sector_return,
                sector_symbol, alpha_vs_spy, alpha_vs_sector, mfe, mae, capture_ratio,
                alpha_score, risk_score, execution_score, composite_score, grade
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trade_id) DO UPDATE SET
                ticker=excluded.ticker, date=excluded.date,
                stock_return=excluded.stock_return, spy_return=excluded.spy_return,
                sector_return=excluded.sector_return, sector_symbol=excluded.sector_symbol,
                alpha_vs_spy=excluded.alpha_vs_spy, alpha_vs_sector=excluded.alpha_vs_sector,
                mfe=excluded.mfe, mae=excluded.mae, capture_ratio=excluded.capture_ratio,
                alpha_score=excluded.alpha_score, risk_score=excluded.risk_score,
                execution_score=excluded.execution_score,
                composite_score=excluded.composite_score, grade=excluded.grade
        """, (
            g["trade_id"], g["ticker"], g["date"], g["stock_return"], g["spy_return"],
            g["sector_return"], g["sector_symbol"], g["alpha_vs_spy"], g["alpha_vs_sector"],
            g["mfe"], g["mae"], g["capture_ratio"], g["alpha_score"], g["risk_score"],
            g["execution_score"], g["composite_score"], g["grade"],
        ))
        conn.commit()
        return True
    except Exception as e:
        logger.error("Failed to save grade: %s", e)
        return False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sector + bars
# ---------------------------------------------------------------------------
def get_sector_etf(ticker: str) -> str:
    ticker = ticker.upper()
    if ticker in SECTOR_MAP:
        return SECTOR_MAP[ticker]
    if "/" in ticker or ticker.endswith("USD"):
        return "BTC/USD"
    if yf is None:
        return "SPY"
    try:
        sector = yf.Ticker(ticker).info.get("sector")
        mapping = {
            "Technology": "XLK", "Communication Services": "XLC",
            "Consumer Cyclical": "XLY", "Consumer Discretionary": "XLY",
            "Financial": "XLF", "Financial Services": "XLF", "Healthcare": "XLV",
            "Energy": "XLE", "Industrials": "XLI", "Basic Materials": "XLB",
            "Materials": "XLB", "Utilities": "XLU", "Real Estate": "XLRE",
            "Consumer Defensive": "XLP", "Consumer Staples": "XLP",
        }
        return mapping.get(sector, "SPY")
    except Exception as e:
        logger.warning("Sector lookup failed for %s: %s", ticker, e)
    return "SPY"


def _download_bars(ticker: str, start, end) -> pd.DataFrame:
    from core.feedback import is_option_contract_symbol
    if yf is None:
        logger.warning("yfinance unavailable; no bars for %s.", ticker)
        return pd.DataFrame()
    sym = ticker
    if "/" in ticker:
        sym = ticker.replace("/", "-")
    try:
        df = yf.download(sym, start=start, end=end, interval="1d", progress=False,
                         auto_adjust=False)
        if not df.empty:
            return df
    except Exception as e:
        logger.warning("yfinance download failed for %s: %s", sym, e)
    # Alpaca fallback could go here in the cloud (needs egress + creds).
    return pd.DataFrame()


def _calculate_return(df: pd.DataFrame) -> float:
    if df is None or df.empty:
        return 0.0
    try:
        if isinstance(df.columns, pd.MultiIndex):
            df = df.copy()
            df.columns = df.columns.get_level_values(0)
        first = df["Open"].dropna().iloc[0] if "Open" in df.columns else df["Close"].dropna().iloc[0]
        last = df["Close"].dropna().iloc[-1] if "Close" in df.columns else df["Open"].dropna().iloc[-1]
        if first > 0:
            return (last - first) / first
    except Exception as e:
        logger.warning("Return calc error: %s", e)
    return 0.0


def _letter_grade(score: float) -> str:
    if score >= 95.0: return "A+"
    if score >= 90.0: return "A"
    if score >= 80.0: return "B"
    if score >= 70.0: return "C"
    if score >= 60.0: return "D"
    return "F"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def grade_trades_for_date(date_str: str, db_path: str = str(DATABASE_PATH)) -> int:
    """Grade all realized_trades closed on ``date_str``. Returns count graded."""
    df = _load_realized_trades(db_path)
    if df is None or df.empty:
        logger.info("No realized trades in %s.", db_path)
        return 0

    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    matching = []
    for _, row in df.iterrows():
        try:
            exit_dt = pd.to_datetime(str(row["exit_date"]))
            if exit_dt.tzinfo is None:
                exit_dt = pytz.utc.localize(exit_dt)
            if exit_dt.astimezone(NY_TZ).date() == target_date:
                matching.append(row.to_dict())
        except Exception as e:
            logger.warning("Bad exit_date %s: %s", row.get("exit_date"), e)

    if not matching:
        logger.info("No trades closed on %s.", date_str)
        return 0

    graded = 0
    for trade in matching:
        try:
            graded += _grade_one(trade, date_str, db_path)
        except Exception as e:
            logger.error("Grading trade %s failed: %s", trade.get("id"), e)
    return graded


def _grade_one(trade: dict, date_str: str, db_path: str) -> int:
    from core.feedback import is_option_contract_symbol, option_underlying
    trade_id = trade["id"]
    ticker = trade["ticker"]
    entry_price = float(trade["entry_price"])
    exit_price = float(trade["exit_price"])

    entry_dt = pd.to_datetime(trade["entry_date"])
    if entry_dt.tzinfo is None:
        entry_dt = pytz.utc.localize(entry_dt)
    exit_dt = pd.to_datetime(trade["exit_date"])
    if exit_dt.tzinfo is None:
        exit_dt = pytz.utc.localize(exit_dt)

    grade_ticker = option_underlying(ticker) if is_option_contract_symbol(ticker) else ticker
    sector_symbol = get_sector_etf(grade_ticker)

    df_stock = _download_bars(grade_ticker, entry_dt, exit_dt)
    df_spy = _download_bars("SPY", entry_dt, exit_dt)
    df_sector = _download_bars(sector_symbol, entry_dt, exit_dt)

    stock_return = (exit_price - entry_price) / entry_price
    spy_return = _calculate_return(df_spy)
    sector_return = _calculate_return(df_sector)
    alpha_vs_spy = stock_return - spy_return
    alpha_vs_sector = stock_return - sector_return

    if not df_stock.empty:
        max_p = max(df_stock["High"].max(), entry_price, exit_price)
        min_p = min(df_stock["Low"].min(), entry_price, exit_price)
    else:
        max_p = max(entry_price, exit_price)
        min_p = min(entry_price, exit_price)
    mfe = (max_p - entry_price) / entry_price
    mae = (min_p - entry_price) / entry_price
    mfe_pct = mfe * 100.0
    stock_return_pct = stock_return * 100.0
    capture_ratio = (stock_return_pct / mfe_pct) if mfe_pct > 0.0001 else 0.0

    # Alpha score (40%)
    alpha_score = 70.0
    avs = alpha_vs_spy * 100.0
    avse = alpha_vs_sector * 100.0
    alpha_score += 2.0 * (avs / 0.1)
    if avse > 0:
        alpha_score += 1.0 * (avse / 0.1)
    alpha_score = max(0.0, min(100.0, alpha_score))

    # Risk score (30%)
    stop_loss_pct = 3.0
    try:
        params = trade.get("parameters") or "{}"
        parsed = json.loads(params) if isinstance(params, str) else (params or {})
        for k in ("trailing_stop_percent", "trailing_stop", "stop_loss_pct", "stop_loss"):
            if k in parsed and parsed[k] is not None:
                v = float(parsed[k])
                stop_loss_pct = (v * 100.0) if v < 1.0 else v
                break
    except Exception:
        pass
    abs_mae = abs(mae * 100.0)
    ratio = abs_mae / stop_loss_pct if stop_loss_pct > 0 else 1.0
    if abs_mae == 0.0:
        risk_score = 100.0
    elif ratio >= 1.0:
        risk_score = 0.0
    elif ratio < 0.95:
        risk_score = 100.0 - 70.0 * (ratio / 0.95)
    else:
        risk_score = 30.0 - 20.0 * ((ratio - 0.95) / 0.05)
    risk_score = max(0.0, min(100.0, risk_score))

    # Execution score (30%)
    if mfe_pct <= 0.0001:
        if stock_return_pct >= -0.05:
            execution_score = 50.0
        else:
            execution_score = max(0.0, 50.0 + stock_return_pct * 10.0)
    else:
        if capture_ratio >= 0.0:
            execution_score = min(100.0, capture_ratio * 100.0)
        else:
            execution_score = max(0.0, 30.0 + capture_ratio * 50.0)
    execution_score = max(0.0, min(100.0, execution_score))

    composite = round(0.40 * alpha_score + 0.30 * risk_score + 0.30 * execution_score, 2)

    g = {
        "trade_id": trade_id, "ticker": ticker, "date": date_str,
        "stock_return": stock_return, "spy_return": spy_return, "sector_return": sector_return,
        "sector_symbol": sector_symbol, "alpha_vs_spy": alpha_vs_spy, "alpha_vs_sector": alpha_vs_sector,
        "mfe": mfe, "mae": mae, "capture_ratio": capture_ratio,
        "alpha_score": alpha_score, "risk_score": risk_score, "execution_score": execution_score,
        "composite_score": composite, "grade": _letter_grade(composite),
    }
    ok = _save_grade(db_path, g)
    if ok:
        logger.info("Grade saved: %s (%s/100) for %s", g["grade"], composite, ticker)
    return 1 if ok else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="D-TAG grader")
    parser.add_argument("--date", default=str(datetime.now(NY_TZ).strftime("%Y-%m-%d")))
    parser.add_argument("--db", default=str(DATABASE_PATH))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    n = grade_trades_for_date(args.date, args.db)
    print(f"graded: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())