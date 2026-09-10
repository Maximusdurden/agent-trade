#!/usr/bin/env python3
"""Per-trade candlestick charts for the Dexter blog (agent-trade).

Ported from dexter-trader's ``core/chart_analyzer.py`` so closed trades get the
same dark candlestick chart with buy/sell markers that readers used to see.
Data comes from agent-trade's broker-authoritative sources:

  - ``core.alpaca_client.AlpacaClient.get_historical_bars`` for OHLC bars
  - ``core.alpaca_client.AlpacaClient.get_executed_orders`` for buy/sell fills

Surface:
    generate_trade_chart(ticker, round_trips, out_dir="reports/charts") -> str|None
        Renders one chart per ticker covering the day's round-trips and returns
        the saved path (or None if no data / render failed).

The chart is a dark mplfinance candlestick with:
  - green up / red down candles
  - a trend (200 EMA) overlay
  - buy (^) and sell (v) markers at the fill prices
  - annotations with qty + price
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

logger = logging.getLogger("Charts")

# mplfinance + matplotlib are runtime/cloud deps (added to requirements.txt).
# Import lazily so other modules can be imported in dev without them installed.
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    import mplfinance as mpf
    _MPL_AVAILABLE = True
except Exception:  # pragma: no cover
    _MPL_AVAILABLE = False
    plt = None
    Line2D = None
    mpf = None

EASTERN_TZ = "America/New_York"
PRE_TRADE_BUFFER_MIN = 45
POST_TRADE_BUFFER_MIN = 45

# Dark mplfinance style (matches dexter's charts).
mc = mpf.make_marketcolors(up="lime", down="red", edge="inherit", wick="inherit", volume="in")
CHART_STYLE = mpf.make_mpf_style(
    marketcolors=mc,
    gridstyle=":",
    gridcolor="#333333",
    facecolor="black",
    figcolor="black",
    rc={
        "axes.labelcolor": "white",
        "xtick.labelcolor": "white",
        "ytick.labelcolor": "white",
        "axes.edgecolor": "white",
        "axes.titlecolor": "white",
        "text.color": "white",
        "legend.facecolor": "black",
        "legend.edgecolor": "gray",
        "legend.labelcolor": "white",
    },
)


def _parse_ts(iso_str):
    """Parse an ISO timestamp to a tz-aware ET pd.Timestamp (naive => UTC)."""
    if not iso_str:
        return None
    try:
        ts = pd.Timestamp(iso_str)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert(EASTERN_TZ)
    except (ValueError, TypeError):
        return None


def _parse_ticker_details(ticker):
    """Return (underlying, asset_type, safe_name, folder_cat) for a symbol."""
    ticker = (ticker or "").upper().replace(" ", "")
    if "/" in ticker:
        return ticker.replace("/", "_"), "CRYPTO", ticker.replace("/", "_"), "Crypto"
    if len(ticker) < 6 and ticker.isalpha():
        return ticker, "STOCK", ticker, "Stocks"
    m = re.search(r"^([A-Z]+)(\d{6})([CP])(\d+)$", ticker)
    if m:
        underlying = m.group(1)
        option_type = "CALL" if m.group(3) == "C" else "PUT"
        folder_cat = "Calls" if option_type == "CALL" else "Puts"
        return underlying, option_type, ticker, folder_cat
    return ticker, "UNKNOWN", ticker, "Other"


def _fetch_bars(client, api_symbol, start_dt, end_dt):
    """Fetch 5-min OHLC bars for the window around a trade (ET-aware)."""
    try:
        fetch_start = start_dt - timedelta(days=2)
        days_diff = (datetime.now() - fetch_start.replace(tzinfo=None)).days + 2
        if days_diff < 1:
            days_diff = 1
        data = client.get_historical_bars(api_symbol, limit=days_diff * 24 * 12, timeframe_str="5min")
        if data is None or data.empty:
            return pd.DataFrame()
        if isinstance(data.index, pd.MultiIndex):
            data = data.droplevel("symbol")
        data.sort_index(inplace=True)
        if data.index.tz is None:
            data.index = data.index.tz_localize("UTC")
        data.index = data.index.tz_convert(EASTERN_TZ)
        data.index = data.index.tz_localize(None)

        data = data[~data.index.duplicated(keep="first")]
        naive_start = start_dt.replace(tzinfo=None)
        naive_end = end_dt.replace(tzinfo=None)
        mask = (data.index >= naive_start) & (data.index <= naive_end)
        data = data.loc[mask]
        if data.empty:
            return pd.DataFrame()
        _, asset_type, _, _ = _parse_ticker_details(api_symbol)
        if asset_type != "CRYPTO":
            data = data.between_time("04:00", "20:00")
        return data
    except Exception as e:
        logger.warning("Bars fetch failed for %s: %s", api_symbol, e)
        return pd.DataFrame()


def _calculate_indicators(data):
    """Add a 200-EMA trend overlay column to the bars df."""
    df = data.copy()
    col_map = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}
    df = df.rename(columns=col_map)
    hlc3 = (df["High"] + df["Low"] + df["Close"]) / 3
    df["trend_ema"] = hlc3.ewm(span=200, adjust=False).mean()
    return df


def _prepare_overlays(client, df, ticker, start_dt, end_dt, round_trips):
    """Build mplfinance addplots + legend for buy/sell markers and trend EMA."""
    ap = []
    legends = []
    annotations = []

    _, asset_type, _, _ = _parse_ticker_details(ticker)
    is_option = asset_type in ("CALL", "PUT")
    if is_option:
        buy_marker, sell_marker = "D", "s"
        buy_color, sell_color = "#00FF00", "#FF0000"
        buy_label, sell_label = "Opt Buy", "Opt Sell"
    else:
        buy_marker, sell_marker = "^", "v"
        buy_color, sell_color = "#00FF00", "#FF0000"
        buy_label, sell_label = "Buy Fill", "Sell Fill"

    # Trend EMA overlay
    if "trend_ema" in df:
        ap.append(mpf.make_addplot(df["trend_ema"], color="orange", width=2.0, alpha=0.7))
        legends.append(Line2D([0], [0], color="orange", linewidth=2, label="Trend (200 EMA)"))

    # Buy/sell markers from the round-trips themselves (entry/exit prices).
    buy_filled = [np.nan] * len(df)
    sell_filled = [np.nan] * len(df)
    has_orders = False
    for rt in round_trips or []:
        open_ts = _parse_ts(rt.get("open_ts"))
        close_ts = _parse_ts(rt.get("close_ts"))
        entry_price = rt.get("entry_price")
        exit_price = rt.get("exit_price")
        qty = rt.get("qty")
        if open_ts is not None and entry_price:
            idx = df.index.get_indexer([open_ts.replace(tzinfo=None)], method="nearest")
            if idx[0] != -1:
                buy_filled[idx[0]] = entry_price
                annotations.append({
                    "x": idx[0], "y": entry_price,
                    "text": f"BUY {qty}\n@{entry_price:.2f}", "color": "#00FF00",
                })
                has_orders = True
        if close_ts is not None and exit_price:
            idx = df.index.get_indexer([close_ts.replace(tzinfo=None)], method="nearest")
            if idx[0] != -1:
                sell_filled[idx[0]] = exit_price
                annotations.append({
                    "x": idx[0], "y": exit_price,
                    "text": f"SELL {qty}\n@{exit_price:.2f}", "color": "#FF4444",
                })
                has_orders = True

    if not all(np.isnan(buy_filled)):
        ap.append(mpf.make_addplot(buy_filled, type="scatter", marker=buy_marker, markersize=120, color=buy_color))
    if not all(np.isnan(sell_filled)):
        ap.append(mpf.make_addplot(sell_filled, type="scatter", marker=sell_marker, markersize=120, color=sell_color))
    if has_orders:
        legends.append(Line2D([0], [0], color="white", marker=buy_marker, markerfacecolor=buy_color, markersize=10, label=buy_label))
        legends.append(Line2D([0], [0], color="white", marker=sell_marker, markerfacecolor=sell_color, markersize=10, label=sell_label))

    return ap, legends, annotations


def _render_chart(title, filename, df, addplots, legends, annotations, save_folder):
    """Render the candlestick chart to ``save_folder/filename``."""
    if not _MPL_AVAILABLE:
        raise RuntimeError("matplotlib/mplfinance required to render trade charts.")
    os.makedirs(save_folder, exist_ok=True)
    full_path = os.path.join(save_folder, filename)
    fig, axlist = mpf.plot(
        df, type="candle", style=CHART_STYLE, addplot=addplots,
        volume=True, panel_ratios=(6, 2),
        figscale=1.5, figsize=(16, 10),
        datetime_format="%H:%M", xrotation=0, returnfig=True,
        warn_too_much_data=10000,
        title=dict(title=title, color="white", fontsize=16, weight="bold"),
    )
    ax = axlist[0]
    if legends:
        ax.legend(handles=legends, loc="upper left", fontsize=10, framealpha=0.6)

    right_edge_x = len(df) + 1
    for i, note in enumerate(annotations):
        if "BUY" in note["text"]:
            y_offset = -40 if i % 2 == 0 else -60
        else:
            y_offset = 40 if i % 2 == 0 else 60
        ax.annotate(
            note["text"],
            xy=(note["x"], note["y"]),
            xytext=(0, y_offset),
            textcoords="offset points",
            arrowprops=dict(arrowstyle="->", color="white", lw=0.8),
            bbox=dict(boxstyle="round,pad=0.3", fc="black", ec=note["color"], alpha=0.8),
            fontsize=8, color="white", ha="center",
        )

    plt.savefig(full_path)
    plt.close(fig)
    logger.info("Generated trade chart: %s", filename)
    return full_path


def generate_trade_chart(ticker: str, round_trips: list[dict], out_dir: str = "reports/charts") -> str | None:
    """Generate one candlestick chart for a ticker's round-trips.

    Args:
        ticker: The symbol (e.g. "COST", "SOL/USD", or an OCC option contract).
        round_trips: The day's closed round-trips for this ticker (feedback shape).
        out_dir: Where to save the PNG.

    Returns the saved path, or None if there's no data / render failed.
    """
    if not _MPL_AVAILABLE:
        logger.warning("matplotlib/mplfinance not installed; skipping chart for %s", ticker)
        return None
    if not round_trips:
        return None

    from core.alpaca_client import get_client_instance
    client = get_client_instance()

    # Window covering all the day's round-trips for this ticker.
    open_ts_list = [_parse_ts(rt.get("open_ts")) for rt in round_trips]
    close_ts_list = [_parse_ts(rt.get("close_ts")) for rt in round_trips]
    valid_ts = [t for t in open_ts_list + close_ts_list if t is not None]
    if not valid_ts:
        return None
    start_dt = min(valid_ts) - timedelta(minutes=PRE_TRADE_BUFFER_MIN)
    end_dt = max(valid_ts) + timedelta(minutes=POST_TRADE_BUFFER_MIN)

    underlying, asset_type, _, folder_cat = _parse_ticker_details(ticker)
    api_symbol = underlying if asset_type in ("CALL", "PUT") else ticker

    data = _fetch_bars(client, api_symbol, start_dt, end_dt)
    if data.empty:
        logger.warning("No bars for %s; skipping chart.", ticker)
        return None

    df = _calculate_indicators(data)
    ap, legends, annotations = _prepare_overlays(client, df, ticker, start_dt, end_dt, round_trips)

    total_pnl = sum(float(rt.get("pnl", 0.0) or 0.0) for rt in round_trips)
    date_str = max(valid_ts).strftime("%Y%m%d")
    filename = f"{ticker.replace('/', '_')}_{date_str}_PNL{int(total_pnl)}.png"
    title = f"{ticker} | {date_str} | PnL: ${total_pnl:.2f}"
    save_folder = os.path.join(out_dir, underlying, folder_cat)
    return _render_chart(title, filename, df, ap, legends, annotations, save_folder)