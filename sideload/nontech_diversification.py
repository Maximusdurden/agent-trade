#!/usr/bin/env python3
"""Non-Tech Sector Diversification Plan — full analysis pipeline.

Evaluates expanding the 3-slot Connors RSI-2 mean-reversion portfolio beyond
mega-cap tech/semis to reduce single-sector concentration.

Steps:
  1. Isolated per-ticker backtest for each candidate with its OWN sector ETF
     macro gate (NOT QQQ/SMH).
  2. Acceptance gates (win rate >= 70%, PF >= 1.50, expectancy >= $15, max DD
     <= 15%).
  3. Concurrency & correlation audit vs the active tech basket.
  4. Multi-sector portfolio simulation (3-slot cap, RSI_2 priority).
  5. Comparative table + decision gate.

Usage:
    python -m sideload.nontech_diversification --no-discord
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message
from sideload.backtest_swing_mean_reversion import (
    load_daily, add_indicators, _signal_met, _stats, BASELINE_CFG,
    load_earnings_dates, _near_earnings,
)

logger = logging.getLogger("NonTechDiversification")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
DATA_DIR = os.path.join(OUT_DIR, "data")
ET = ZoneInfo("America/New_York")

LOOKBACK_DAYS = 365 * 8

# --- Active tech basket (current production universe) ---
TECH_UNIVERSE = [
    ("AMD", "SMH"), ("NVDA", "SMH"), ("TSLA", "SMH"), ("SMH", "SMH"),
    ("MSFT", "QQQ"), ("AAPL", "QQQ"), ("GOOGL", "QQQ"), ("META", "QQQ"),
]

# --- Non-tech candidates mapped to their PRIMARY sector ETF macro gate ---
# NOTE: directive said "DEX" for Industrials but the real Deere ticker is "DE"
# (DEX is a delisted data artifact ending 2023-03-10 with ~200-share volume).
NONTECH_UNIVERSE = [
    ("JPM", "XLF"),   # Financials
    ("GS", "XLF"),    # Financials
    ("CAT", "XLI"),   # Industrials
    ("DE", "XLI"),    # Industrials (Deere; directive's "DEX" is a typo)
    ("UNH", "XLV"),   # Healthcare
    ("ABT", "XLV"),   # Healthcare
    ("XOM", "XLE"),   # Energy (control/test)
]

MAX_SLOTS = 3
SLOT_SIZE_PCT = 0.33
EQUITY = 10000.0

# Acceptance gates (per-asset).
GATES = {
    "win_rate_min": 0.70,
    "profit_factor_min": 1.50,
    "expectancy_usd_min": 15.00,
    "max_dd_pct_max": 15.0,
}


def _rsi2(close: pd.Series) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / 2, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / 2, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def _stretch_units(df: pd.DataFrame, ts) -> float:
    """(Close - SMA20) / ATR14 at ts (negative = stretched below SMA20)."""
    try:
        loc = df.index.get_loc(ts)
    except Exception:
        return 0.0
    if loc < 20:
        return 0.0
    row = df.iloc[loc]
    return float((row["close"] - row["sma20"]) / row["atr"]) if row["atr"] > 0 else 0.0


def _load_all(client: AlpacaClient, symbols: list[tuple[str, str]]) -> tuple[dict, dict]:
    """Load + add indicators for all symbols and their sector gates."""
    data = {}
    sector_data = {}
    for sym, sector in symbols:
        df = load_daily(client, sym, LOOKBACK_DAYS)
        if df.empty:
            continue
        data[sym] = add_indicators(df, dict(BASELINE_CFG))
        if sector not in sector_data:
            sdf = load_daily(client, sector, LOOKBACK_DAYS)
            if not sdf.empty:
                sector_data[sector] = add_indicators(sdf, dict(BASELINE_CFG))
    return data, sector_data


def isolated_backtest(client: AlpacaClient, sym: str, sector: str,
                      earnings_dates: set | None = None) -> dict:
    """Run an isolated 8-year backtest on ONE ticker with its OWN sector gate.

    Entry: RSI_2 < 10 AND Close_t-1 > SMA200_t-1 AND Sector_ETF_t-1 > SMA200_t-1.
    Execution: next-day open + $0.05 slippage.
    Exit: Close > SMA5 (close window), hold >= 5 days, or Cat Stop at
          Entry - 2.0*ATR14.
    Earnings filter: block setups within +/-2 days of earnings.
    """
    df = load_daily(client, sym, LOOKBACK_DAYS)
    if df.empty:
        return {"symbol": sym, "error": "no data", "trades": 0}
    sdf = load_daily(client, sector, LOOKBACK_DAYS)
    if sdf.empty:
        return {"symbol": sym, "error": f"no sector data {sector}", "trades": 0}

    cfg = dict(BASELINE_CFG)
    cfg["gate_mode"] = "sector"
    cfg["sector_df"] = sdf

    trades = _simulate_isolated(df, cfg, earnings_dates=earnings_dates)
    stats = _stats(trades, cfg)
    stats.update({
        "symbol": sym, "sector": sector, "baseline": True,
        "date_range": f"{df.index[0].date()} to {df.index[-1].date()}",
        "trades_list": trades,
    })
    return stats


def _simulate_isolated(df: pd.DataFrame, cfg: dict,
                       earnings_dates: set | None = None,
                       earnings_window: int = 2) -> list[dict]:
    """Leakage-free isolated simulation with sector gate (adapted from simulate)."""
    df = add_indicators(df, cfg)
    sig_cols = ["close", "rsi", "boll_lower", "sma20", "atr", "sma_trend",
                "sma50", "sma_trend_5ago", "sma5"]
    sig = df[sig_cols].shift(1)

    sector_df = cfg.get("sector_df")
    if sector_df is not None and not sector_df.empty:
        sdf = add_indicators(sector_df, cfg)
        s_close = sdf["close"].reindex(df.index).ffill()
        s_trend = sdf["sma_trend"].reindex(df.index).ffill()
        sig["sector_close"] = s_close.shift(1)
        sig["sector_sma_trend"] = s_trend.shift(1)
    else:
        sig["sector_close"] = np.nan
        sig["sector_sma_trend"] = np.nan

    trades = []
    i = 0
    n = len(df)
    while i < n:
        ts = df.index[i]
        row = df.iloc[i]
        if i >= 1 and _signal_met(sig.iloc[i], cfg, True):
            if earnings_dates and _near_earnings(ts, earnings_dates, earnings_window):
                i += 1
                continue
            entry = float(row["open"]) + float(cfg["slippage"])
            if entry <= 0:
                i += 1
                continue
            entry_ts = ts
            entry_atr = float(sig.iloc[i]["atr"])
            cat_mult = float(cfg.get("catastrophic_atr_mult", 2.0))
            cat_stop = entry - cat_mult * entry_atr
            exit_reason = None
            exit_px = None
            exit_ts = None
            for j in range(i + 1, min(i + 1 + int(cfg["max_hold_days"]), n)):
                bar = df.iloc[j]
                if float(bar["close"]) > float(sig.iloc[j]["sma5"]):
                    exit_reason = "sma5_touch"
                    exit_px = float(bar["close"])
                    exit_ts = df.index[j]
                    break
                if float(bar["low"]) <= cat_stop:
                    exit_reason = "cat_stop"
                    exit_px = cat_stop
                    exit_ts = df.index[j]
                    break
            if exit_reason is None:
                j = min(i + int(cfg["max_hold_days"]), n - 1)
                exit_reason = "time_stop"
                exit_px = float(df.iloc[j]["close"])
                exit_ts = df.index[j]
            ret = (exit_px - entry) / entry
            trades.append({
                "entry_ts": str(entry_ts.date()), "exit_ts": str(exit_ts.date()),
                "entry": entry, "exit": exit_px,
                "ret_pct": ret * 100.0, "exit_reason": exit_reason,
                "hold_days": (exit_ts - entry_ts).days,
            })
            i = j + 1
        else:
            i += 1
    return trades


def check_gates(stats: dict) -> dict:
    """Evaluate acceptance gates for a single asset."""
    if "error" in stats or stats.get("trades", 0) == 0:
        return {"passed": False, "reason": "no trades"}
    checks = {
        "win_rate": stats["win_rate"] >= GATES["win_rate_min"],
        "profit_factor": stats["profit_factor"] >= GATES["profit_factor_min"],
        "expectancy": stats["expectancy_usd"] >= GATES["expectancy_usd_min"],
        "max_dd": stats["max_drawdown_pct"] <= GATES["max_dd_pct_max"],
    }
    passed = all(checks.values())
    return {
        "passed": passed,
        "checks": checks,
        "values": {
            "win_rate": stats["win_rate"],
            "profit_factor": stats["profit_factor"],
            "expectancy": stats["expectancy_usd"],
            "max_dd": stats["max_drawdown_pct"],
        },
    }


def concurrency_audit(tech_trades: dict[str, list[dict]],
                      nontech_trades: dict[str, list[dict]]) -> dict:
    """Audit trade-timing overlap between non-tech and tech baskets."""
    tech_dates = set()
    for sym, trades in tech_trades.items():
        for t in trades:
            tech_dates.add(pd.Timestamp(t["entry_ts"]).normalize())
    result = {}
    for sym, trades in nontech_trades.items():
        if not trades:
            result[sym] = {"n": 0, "overlap": 0.0}
            continue
        overlap = sum(1 for t in trades
                      if pd.Timestamp(t["entry_ts"]).normalize() in tech_dates)
        result[sym] = {
            "n": len(trades),
            "overlap": overlap / len(trades) * 100.0,
        }
    return result


def run_portfolio(client: AlpacaClient, symbols: list[tuple[str, str]],
                  max_slots: int = MAX_SLOTS,
                  slot_size_pct: float = SLOT_SIZE_PCT,
                  equity: float = EQUITY) -> dict:
    """Run the 3-slot priority portfolio across the given universe.

    Priority: lowest RSI_2 first, tie-break by stretch below SMA20.
    """
    data, sector_data = _load_all(client, symbols)
    if not data:
        return {"error": "no data", "trades": 0}

    all_days = sorted(set().union(*[set(df.index.normalize()) for df in data.values()]))
    all_days = [pd.Timestamp(d) for d in all_days]

    positions = {}
    trades = []
    slot_days = 0

    for i, day in enumerate(all_days):
        day_naive = day.tz_localize(None).normalize()
        # 1. Exits.
        for sym in list(positions.keys()):
            df = data[sym]
            day_bars = df[df.index.normalize() == day]
            if day_bars.empty:
                continue
            pos = positions[sym]
            last = day_bars.iloc[-1]
            close = float(last["close"])
            sma5 = float(last["sma5"])
            low = float(last["low"])
            exit_reason = None
            exit_px = None
            if close > sma5:
                exit_reason, exit_px = "sma5_touch", close
            elif low <= pos["cat_stop"]:
                exit_reason, exit_px = "cat_stop", pos["cat_stop"]
            elif (day_naive - pos["day0"]).days >= 5:
                exit_reason, exit_px = "time_stop", close
            if exit_reason:
                ret = (exit_px - pos["entry"]) / pos["entry"]
                trades.append({
                    "symbol": sym, "entry_ts": str(pos["entry_ts"].date()),
                    "exit_ts": str(day_naive.date()), "entry": pos["entry"],
                    "exit": exit_px, "ret_pct": ret * 100.0,
                    "exit_reason": exit_reason,
                    "hold_days": (day_naive - pos["day0"]).days,
                })
                slot_days += (day_naive - pos["day0"]).days
                del positions[sym]

        # 2. Entries.
        open_slots = max_slots - len(positions)
        if open_slots <= 0:
            continue
        contenders = []
        for sym in data:
            if sym in positions:
                continue
            df = data[sym]
            sig = df[["close", "rsi", "boll_lower", "sma20", "atr", "sma_trend",
                      "sma50", "sma_trend_5ago", "sma5"]].shift(1)
            prev_idx = all_days[i - 1] if i > 0 else None
            if prev_idx is None:
                continue
            prev_naive = prev_idx.tz_localize(None).normalize()
            sig_idx = sig.index.tz_localize(None).normalize()
            sig_row = sig[sig_idx == prev_naive]
            if sig_row.empty:
                continue
            row = sig_row.iloc[-1]
            # Sector gate (own primary sector ETF).
            sector = dict(symbols)[sym]
            sdf = sector_data.get(sector)
            if sdf is None:
                continue
            s_sig = sdf[["close", "sma_trend"]].shift(1)
            s_idx = s_sig.index.tz_localize(None).normalize()
            s_row = s_sig[s_idx == prev_naive]
            if s_row.empty:
                continue
            sector_ok = float(s_row.iloc[-1]["close"]) > float(s_row.iloc[-1]["sma_trend"])
            if not sector_ok:
                continue
            if not (row["rsi"] < BASELINE_CFG["rsi_buy_below"]):
                continue
            if not (row["close"] > row["sma_trend"]):
                continue
            entry_bars = df[df.index.normalize() == day]
            if entry_bars.empty:
                continue
            contenders.append({
                "symbol": sym, "rsi": float(row["rsi"]),
                "stretch": _stretch_units(df, prev_naive),
                "entry_open": float(entry_bars.iloc[0]["open"]),
                "atr": float(row["atr"]),
            })

        # 3. Priority: lowest RSI_2 first, tie-break by stretch.
        contenders.sort(key=lambda c: (c["rsi"], c["stretch"]))
        for c in contenders[:open_slots]:
            if c["entry_open"] <= 0:
                continue
            entry = c["entry_open"] + BASELINE_CFG["slippage"]
            positions[c["symbol"]] = {
                "entry_ts": day, "entry": entry,
                "cat_stop": entry - 2.0 * c["atr"], "day0": day_naive,
            }

    # Close open positions.
    for sym, pos in positions.items():
        df = data[sym]
        last = float(df["close"].iloc[-1])
        ret = (last - pos["entry"]) / pos["entry"]
        trades.append({
            "symbol": sym, "entry_ts": str(pos["entry_ts"].date()),
            "exit_ts": str(all_days[-1].tz_localize(None).date()),
            "entry": pos["entry"], "exit": last, "ret_pct": ret * 100.0,
            "exit_reason": "end_of_data",
            "hold_days": (all_days[-1].tz_localize(None) - pos["day0"]).days,
        })
        slot_days += (all_days[-1].tz_localize(None) - pos["day0"]).days

    if not trades:
        return {"error": "no trades", "trades": 0}
    rets = np.array([t["ret_pct"] for t in trades])
    size_usd = slot_size_pct * equity
    pnls = rets / 100.0 * size_usd
    wins = rets > 0
    pnl_by_day = {}
    for t in trades:
        d = pd.Timestamp(t["exit_ts"]).tz_localize(None).normalize()
        pnl_by_day[d] = pnl_by_day.get(d, 0.0) + t["ret_pct"] / 100.0 * size_usd
    days_arr = [d.tz_localize(None).normalize() for d in all_days]
    eq = np.full(len(days_arr), equity, dtype=float)
    for k, d in enumerate(days_arr):
        if k > 0:
            eq[k] = eq[k - 1]
        if d in pnl_by_day:
            eq[k] += pnl_by_day[d]
    eq_s = pd.Series(eq, index=days_arr)
    peak = eq_s.cummax()
    dd = (eq_s - peak) / peak
    max_dd = float(abs(dd.min()) * 100.0)
    ann = (eq_s.iloc[-1] / eq_s.iloc[0]) ** (1.0 / (len(days_arr) / 252.0)) - 1.0
    rets_d = eq_s.pct_change().dropna()
    sharpe = float(rets_d.mean() / rets_d.std() * np.sqrt(252)) if rets_d.std() > 0 else 0.0
    calmar = ann / (max_dd / 100.0) if max_dd > 0 else 0.0
    total_days = len(days_arr)
    exposure = slot_days / (total_days * max_slots) * 100.0

    by_sym = {}
    for sym in dict.fromkeys(t["symbol"] for t in trades):
        st = [t for t in trades if t["symbol"] == sym]
        sr = np.array([t["ret_pct"] for t in st])
        by_sym[sym] = {
            "trades": len(st), "win": float((sr > 0).mean()),
            "exp": float((sr / 100 * size_usd).mean()),
            "total": float((sr / 100 * size_usd).sum()),
        }

    return {
        "universe": [s for s, _ in symbols],
        "max_slots": max_slots, "slot_size_pct": slot_size_pct,
        "total_trades": len(trades), "win_rate": float(wins.mean()),
        "expectancy_usd": float(pnls.mean()), "total_pnl_usd": float(pnls.sum()),
        "max_drawdown_pct": max_dd, "annualized_return_pct": ann * 100.0,
        "sharpe": sharpe, "calmar": calmar,
        "exposure_pct": exposure, "total_days": total_days,
        "by_symbol": by_sym,
        "exit_reasons": {r: sum(1 for t in trades if t["exit_reason"] == r) for r in set(t["exit_reason"] for t in trades)},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Non-tech sector diversification analysis")
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-nontech-diversification")
    client = AlpacaClient()

    try:
        report = {}

        # ---- Step 1 & 2: Isolated backtests + acceptance gates ----
        print("\n" + "=" * 72)
        print("STEP 1-2: ISOLATED BACKTESTS + ACCEPTANCE GATES")
        print("=" * 72)
        isolated = {}
        gate_results = {}
        for sym, sector in NONTECH_UNIVERSE:
            earnings = load_earnings_dates(sym, LOOKBACK_DAYS)
            stats = isolated_backtest(client, sym, sector, earnings_dates=earnings)
            isolated[sym] = stats
            gates = check_gates(stats)
            gate_results[sym] = gates
            if "error" in stats:
                print(f"  {sym} ({sector}): ERROR {stats['error']}")
                continue
            print(f"  {sym} ({sector}): n={stats['trades']} win={stats['win_rate']:.1%} "
                  f"PF={stats['profit_factor']:.2f} exp=${stats['expectancy_usd']:.2f} "
                  f"maxDD={stats['max_drawdown_pct']:.1f}% -> {'PASS' if gates['passed'] else 'FAIL'}")
            if not gates["passed"]:
                for k, v in gates["checks"].items():
                    if not v:
                        print(f"      FAIL {k}: {gates['values'][k]:.3f}")

        # ---- Step 3: Concurrency audit ----
        print("\n" + "=" * 72)
        print("STEP 3: CONCURRENCY & CORRELATION AUDIT")
        print("=" * 72)
        # Tech basket trades (isolated, for date overlap).
        tech_trades = {}
        for sym, sector in TECH_UNIVERSE:
            earnings = load_earnings_dates(sym, LOOKBACK_DAYS)
            stats = isolated_backtest(client, sym, sector, earnings_dates=earnings)
            tech_trades[sym] = stats.get("trades_list", [])
        nontech_trades = {sym: isolated[sym].get("trades_list", []) for sym in isolated}
        conc = concurrency_audit(tech_trades, nontech_trades)
        for sym, r in conc.items():
            print(f"  {sym}: {r['n']} trades, {r['overlap']:.1f}% overlap with tech")
        report["concurrency"] = conc

        # ---- Step 4: Portfolio simulations ----
        print("\n" + "=" * 72)
        print("STEP 4: MULTI-SECTOR PORTFOLIO SIMULATION")
        print("=" * 72)
        # Tech-only baseline.
        tech_port = run_portfolio(client, TECH_UNIVERSE)
        print(f"  TECH-ONLY: {tech_port['total_trades']} trades, ann={tech_port['annualized_return_pct']:.2f}%, "
              f"Sharpe={tech_port['sharpe']:.2f}, maxDD={tech_port['max_drawdown_pct']:.2f}%, "
              f"exposure={tech_port['exposure_pct']:.1f}%, exp=${tech_port['expectancy_usd']:.2f}")
        report["tech_only"] = tech_port

        # Expanded: tech + passing non-tech candidates.
        passing = [sym for sym, g in gate_results.items() if g["passed"]]
        expanded_universe = TECH_UNIVERSE + [(s, dict(NONTECH_UNIVERSE)[s]) for s in passing]
        print(f"  Passing non-tech: {passing}")
        if passing:
            expanded_port = run_portfolio(client, expanded_universe)
            print(f"  EXPANDED ({len(expanded_universe)} tickers): {expanded_port['total_trades']} trades, "
                  f"ann={expanded_port['annualized_return_pct']:.2f}%, Sharpe={expanded_port['sharpe']:.2f}, "
                  f"maxDD={expanded_port['max_drawdown_pct']:.2f}%, exposure={expanded_port['exposure_pct']:.1f}%, "
                  f"exp=${expanded_port['expectancy_usd']:.2f}")
            report["expanded"] = expanded_port

        # ---- Step 5: Comparative table ----
        print("\n" + "=" * 72)
        print("STEP 5: COMPARATIVE TABLE")
        print("=" * 72)
        t = tech_port
        e = report.get("expanded")
        print(f"  {'Metric':<28} | {'Tech-Only':>14} | {'Expanded':>14}")
        print(f"  {'-'*28}-+-{'-'*14}-+-{'-'*14}")
        print(f"  {'Annualized Return %':<28} | {t['annualized_return_pct']:>13.2f}% | {e['annualized_return_pct'] if e else 0:>13.2f}%")
        print(f"  {'Portfolio Sharpe':<28} | {t['sharpe']:>14.2f} | {e['sharpe'] if e else 0:>14.2f}")
        print(f"  {'Portfolio Max DD %':<28} | {t['max_drawdown_pct']:>13.2f}% | {e['max_drawdown_pct'] if e else 0:>13.2f}%")
        print(f"  {'Capital Exposure %':<28} | {t['exposure_pct']:>13.2f}% | {e['exposure_pct'] if e else 0:>13.2f}%")
        print(f"  {'Mean Expectancy / Trade':<28} | ${t['expectancy_usd']:>13.2f} | ${e['expectancy_usd'] if e else 0:>13.2f}")

        # Decision gate.
        if e:
            dd_ok = e["max_drawdown_pct"] < 10.0
            sharpe_ok = e["sharpe"] >= 1.35
            diluted = e["sharpe"] < 1.25
            print("\n  DECISION GATE:")
            print(f"    Expanded maxDD {e['max_drawdown_pct']:.2f}% < 10%? {'YES' if dd_ok else 'NO'}")
            print(f"    Expanded Sharpe {e['sharpe']:.2f} >= 1.35? {'YES' if sharpe_ok else 'NO'}")
            print(f"    Sharpe diluted below 1.25? {'YES' if diluted else 'NO'}")
            if dd_ok and sharpe_ok and not diluted:
                print("    -> RECOMMENDATION: EXPAND UNIVERSE (update core/strategies/swing_rsi2_mean_reversion.py)")
            else:
                print("    -> RECOMMENDATION: PRESERVE 8-TICKER TECH CORE")

        # Save report.
        path = os.path.join(DATA_DIR, "nontech_diversification.json")
        os.makedirs(DATA_DIR, exist_ok=True)
        # Strip trades_list for compactness.
        compact = json.loads(json.dumps(report, default=str))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(compact, fh, indent=2, default=str)
        logger.info(f"Wrote {path}")

        if not args.no_discord:
            try:
                send_discord_message(
                    f"Non-tech diversification: tech Sharpe={t['sharpe']:.2f} "
                    f"expanded={e['sharpe'] if e else 'N/A'}, "
                    f"passing={passing}")
            except Exception as ex:
                logger.warning(f"Discord failed: {ex}")
    except Exception as e:
        logger.critical(f"Non-tech diversification analysis failed: {e}")
        log_exception_to_jira(e, "Non-Tech Diversification Analysis Failure")
        raise


if __name__ == "__main__":
    main()