#!/usr/bin/env python3
"""
Cloud Agent vs Major Indices Performance Comparison.

Loads the cloud agent's equity curve from portfolio_history (in
cloud_downloaded_trading_agent.db, pulled from GCS), fetches benchmark index
data (SPY, QQQ, DIA, IWM, BTC/USD, ETH/USD) via Alpaca for the same window,
normalizes all to a common start (indexed to 100), and produces:
  - reports/cloud_performance_vs_indices.md
  - reports/cloud_performance_vs_indices.csv
  - reports/cloud_performance_vs_indices.png

Read-only: does not modify any database or trading state.
"""
import os
import sys
import sqlite3
import logging
from datetime import timedelta

import pandas as pd
import numpy as np

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CLOUD_DB = os.path.join(PROJECT_ROOT, "cloud_downloaded_trading_agent.db")
# Dexter DB path (downtime: same-machine to allow cross-project comparison)
DEXTER_DB = os.environ.get(
    "DEXTER_DB_PATH",
    r"Z:\python\projects\dexter-trader\dexter-trader.db",
)
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Benchmarks via Alpaca: (display_name, symbol, is_crypto)
BENCHMARKS = [
    ("SPY", "SPY", False),
    ("QQQ", "QQQ", False),
    ("DIA", "DIA", False),
    ("IWM", "IWM", False),
    ("BTC", "BTC/USD", True),
    ("ETH", "ETH/USD", True),
]


def load_cloud_equity():
    conn = sqlite3.connect(CLOUD_DB)
    df = pd.read_sql_query(
        "SELECT timestamp, equity FROM portfolio_history ORDER BY timestamp ASC", conn
    )
    conn.close()
    df["date"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    df = df.dropna(subset=["date", "equity"])
    df = df.drop_duplicates(subset="date", keep="last").set_index("date").sort_index()
    # Resample to daily (last equity of each day) for cleaner comparison
    df = df["equity"].resample("D").last().dropna()
    # Normalize to tz-naive dates
    df.index = df.index.tz_localize(None).normalize()
    return df


def load_dexter_equity(start, end):
    """Load Dexter's daily equity from daily_account_metrics in the given window."""
    if not os.path.exists(DEXTER_DB):
        logger.warning(f"Dexter DB not found at {DEXTER_DB}. Skipping Dexter series.")
        return None
    conn = sqlite3.connect(DEXTER_DB)
    df = pd.read_sql_query(
        "SELECT date, equity FROM daily_account_metrics ORDER BY date ASC", conn
    )
    conn.close()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "equity"])
    df = df.drop_duplicates(subset="date", keep="last").set_index("date").sort_index()
    df.index = df.index.normalize()
    # Filter out known corrupted/adversarial snapshots (e.g. 2026-07-07 $39k is a
    # glitch between two ~$102k values). Drop points that deviate >40% from the
    # median of neighbors (in either direction) -- they reflect bad broker
    # snapshots, not real PnL.
    eq = df["equity"].astype(float)
    med = eq.rolling(3, center=True, min_periods=1).median()
    ratio = eq / med
    valid_idx = (ratio >= 0.60) & (ratio <= 1.60)
    excluded = df.index[~valid_idx]
    if len(excluded):
        logger.info(f"Dexter: dropping {len(excluded)} anomalous snapshot(s): {list(excluded.astype(str))}")
    df = df.loc[valid_idx]

    # Restrict to the cloud window (inclusive)
    df = df["equity"]
    pre = df[df.index < pd.Timestamp(start)]
    mask = (df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))
    window = df[mask]
    # Anchor to the last valid equity before the window start so the comparison
    # window truly reflects returns from the same starting baseline.
    if not pre.empty and not window.empty:
        baseline = pre.iloc[-1]
        window = pd.concat([pd.Series({pre.index[-1]: baseline}), window])
    if window.empty:
        logger.warning("No Dexter equity in the cloud window.")
        return None
    return window


def load_benchmark(symbol, is_crypto, start, end):
    from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed
    from core.config import ALPACA_API_KEY, ALPACA_SECRET_KEY

    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    start_dt = start_ts if start_ts.tz is not None else start_ts.tz_localize("UTC")
    end_dt = end_ts if end_ts.tz is not None else end_ts.tz_localize("UTC")
    end_dt = end_dt + timedelta(days=1)
    try:
        if is_crypto:
            from alpaca.data.historical import CryptoHistoricalDataClient
            client = CryptoHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
            req = CryptoBarsRequest(symbol_or_symbols=symbol,
                                    timeframe=TimeFrame(1, TimeFrameUnit.Day),
                                    start=start_dt, end=end_dt)
            df = client.get_crypto_bars(req).df
        else:
            from alpaca.data.historical import StockHistoricalDataClient
            client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
            req = StockBarsRequest(symbol_or_symbols=symbol,
                                   timeframe=TimeFrame(1, TimeFrameUnit.Day),
                                   start=start_dt, end=end_dt, feed=DataFeed.IEX)
            df = client.get_stock_bars(req).df
        if df is None or df.empty:
            return None
        df = df.reset_index()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp").sort_index()
        close = df["close"].dropna()
        close = close[~close.index.duplicated(keep="last")]
        # Normalize to tz-naive daily dates to match cloud equity
        close.index = close.index.tz_localize(None).normalize()
        return close
    except Exception as e:
        logger.warning(f"Failed to fetch {symbol}: {e}")
        return None


def compute_metrics(series):
    s = series.dropna()
    if len(s) < 2:
        return None
    total_return = s.iloc[-1] / s.iloc[0] - 1.0
    years = max((s.index[-1] - s.index[0]).days / 365.25, 1e-9)
    cagr = (s.iloc[-1] / s.iloc[0]) ** (1 / years) - 1.0
    rets = s.pct_change().dropna()
    vol = rets.std() * np.sqrt(252)
    sharpe = (rets.mean() * 252) / (rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0
    max_dd = (s / s.cummax() - 1.0).min()
    return {
        "total_return": total_return, "cagr": cagr, "volatility": vol,
        "sharpe": sharpe, "max_drawdown": max_dd,
        "start": s.index[0].strftime("%Y-%m-%d"), "end": s.index[-1].strftime("%Y-%m-%d"),
    }


def main():
    logger.info("Loading cloud equity curve...")
    cloud = load_cloud_equity()
    if cloud.empty:
        logger.error("No portfolio_history found in cloud DB.")
        sys.exit(1)
    start = cloud.index[0].date()
    end = cloud.index[-1].date()
    logger.info(f"Cloud equity window: {start} -> {end} ({len(cloud)} days)")
    logger.info(f"Cloud equity {cloud.iloc[0]:,.2f} -> {cloud.iloc[-1]:,.2f}")

    combined = pd.DataFrame({"Cloud": cloud / cloud.iloc[0] * 100.0})
    for label, symbol, is_crypto in BENCHMARKS:
        logger.info(f"Fetching {label} ({symbol})...")
        close = load_benchmark(symbol, is_crypto, start, end)
        if close is not None:
            norm = close / close.iloc[0] * 100.0
            norm.name = label
            combined = combined.join(norm, how="outer")

    # Add Dexter for a like-for-like comparison over the same window
    logger.info("Loading Dexter equity curve...")
    dexter = load_dexter_equity(start, end)
    if dexter is not None:
        dexter_norm = dexter / dexter.iloc[0] * 100.0
        dexter_norm.name = "Dexter"
        combined = combined.join(dexter_norm, how="outer")

    combined = combined.sort_index().ffill().dropna(how="all")

    metrics_rows = []
    for col in combined.columns:
        m = compute_metrics(combined[col])
        if m:
            metrics_rows.append({"series": col, **m})
    metrics_df = pd.DataFrame(metrics_rows)

    combined.to_csv(os.path.join(REPORTS_DIR, "cloud_performance_vs_indices.csv"))

    fig, ax = plt.subplots(figsize=(12, 7))
    colors = {"Cloud": "#d62728", "Dexter": "#e377c2", "SPY": "#1f77b4", "QQQ": "#2ca02c",
              "DIA": "#9467bd", "IWM": "#8c564b", "BTC": "#ff7f0e", "ETH": "#17becf"}
    for col in combined.columns:
        ax.plot(combined.index, combined[col], label=col,
                linewidth=2.4 if col in ("Cloud", "Dexter") else 1.4, color=colors.get(col))
    ax.set_title("Cloud vs Dexter vs Major Indices (Indexed to 100)")
    ax.set_ylabel("Indexed Value (start = 100)")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    chart_path = os.path.join(REPORTS_DIR, "cloud_performance_vs_indices.png")
    fig.savefig(chart_path, dpi=150)
    plt.close(fig)

    lines = []
    lines.append("# Cloud Trading Agent vs Dexter vs Major Indices\n")
    lines.append(f"**Analysis window:** {start} to {end}\n")
    lines.append(f"**Cloud starting equity:** ${cloud.iloc[0]:,.2f}  ")
    lines.append(f"**Cloud ending equity:** ${cloud.iloc[-1]:,.2f}\n")
    lines.append("## Summary Table\n")
    lines.append("| Series | Total Return | CAGR | Volatility | Sharpe | Max Drawdown |")
    lines.append("|--------|-------------|------|-----------|--------|--------------|")
    for _, r in metrics_df.sort_values("total_return", ascending=False).iterrows():
        lines.append(
            f"| {r['series']} | {r['total_return']*100:+.2f}% | {r['cagr']*100:+.2f}% | "
            f"{r['volatility']*100:.2f}% | {r['sharpe']:.2f} | {r['max_drawdown']*100:.2f}% |"
        )
    lines.append("\n## Chart\n")
    lines.append("![Cloud vs Indices](cloud_performance_vs_indices.png)\n")
    lines.append("## Notes\n")
    lines.append("- Cloud equity is the daily equity from `portfolio_history` (authoritative cloud DB from GCS).")
    lines.append("- Dexter equity is from `daily_account_metrics` in the dexter-trader DB, restricted to the same window.")
    lines.append("- Benchmarks use Alpaca daily close (IEX feed for equities).")
    lines.append("- All series indexed to 100 at start.\n")

    report_path = os.path.join(REPORTS_DIR, "cloud_performance_vs_indices.md")
    with open(report_path, "w") as f:
        f.write("\n".join(lines))
    logger.info(f"Report saved to {report_path}")

    print("\n=== CLOUD VS DEXTER VS INDICES SUMMARY ===")
    print(metrics_df.sort_values("total_return", ascending=False).to_string(index=False))


if __name__ == "__main__":
    main()