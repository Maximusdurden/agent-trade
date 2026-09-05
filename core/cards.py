#!/usr/bin/env python3
"""Recreated dashboard card / calendar / sidebar renderers for the blog.

These render the visual dashboard assets the blog embeds, but compute all numbers
from agent-trade data via ``core.blog_stats`` (instead of dexter's raw tables).
They were recreated (not blindly ported) per the migration plan §4.3.

Surface:
    render_performance_card(stats, streak_map, day_pnl, out_path, title_date=None)
    generate_yearly_calendar_html(df, year) -> str
    render_sidebar_image(...) -> path  (thin wrapper over the card)
"""

from __future__ import annotations

import calendar as _calendar
import logging
import os
from typing import Optional

import pandas as pd

logger = logging.getLogger("Cards")

# PIL (Pillow) is a runtime/cloud dependency (added in requirements.txt). Import it
# lazily so other modules (e.g. tools.blog_update) can be imported in dev without
# Pillow installed; the card renderers will fail loudly only when actually used.
try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PIL_AVAILABLE = False
    Image = ImageDraw = ImageFont = None

# Dexter card palette (white / teal / brown) — keep for a seamless look.
TEXT_COLOR = "#333333"
TEAL = "#008080"
BROWN = "#5d4037"
GREEN = "#2e7d32"
RED = "#c62828"
LINE = "#e0e0e0"

_FONT_CACHE = {}
_FONT_PATHS = [
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _font(size: int, bold: bool = False):
    """Load a font (unicode-compatible), preferring system fonts; fallback to default."""
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    img = Image.new("RGB", (10, 10))
    fallback = ImageFont.load_default()
    f = None
    for path in _FONT_PATHS:
        if "Bold" in path or path.endswith("arialbd.ttf"):
            continue  # handled via arial.ttf below for cross-platform simplicity
        try:
            f = ImageFont.truetype(path, size)
            break
        except Exception:
            continue
    if f is None:
        f = fallback
    _FONT_CACHE[key] = f
    return f


def render_performance_card(stats: dict, streak_map: list, day_pnl: float,
                            out_path: str, title_date: Optional[str] = None) -> str:
    """Render the 400x600 'Dexter' summary card to ``out_path`` and return the path."""
    if not _PIL_AVAILABLE:
        raise RuntimeError(
            "Pillow is required to render the performance card. "
            "Install it (requirements.txt) or run in the cloud image.")
    width, height = 400, 600
    img = Image.new("RGB", (width, height), color="#ffffff")
    draw = ImageDraw.Draw(img)

    font_main = _font(30)
    font_sub = _font(20)
    font_metrics = _font(18)
    font_label = _font(16)

    # Header
    draw.text((115, 30), "DEXTER BOT", fill=TEAL, font=font_main)
    if title_date:
        draw.text((150, 75), str(title_date), fill=BROWN, font=font_label)
    draw.line((40, 100, 360, 100), fill=TEAL, width=3)

    # Account balance
    draw.text((40, 130), "Acct Bal:", fill=TEXT_COLOR, font=font_sub)
    draw.text((250, 130), f"${stats.get('est_balance', 0):,.0f}", fill=BROWN, font=font_sub)

    # Today PnL
    day_pnl = float(day_pnl or 0.0)
    pnl_color = GREEN if day_pnl >= 0 else RED
    arrow = "▲" if day_pnl >= 0 else "▼"
    draw.text((40, 170), "Today PnL:", fill=TEXT_COLOR, font=font_sub)
    draw.text((250, 170), f"{arrow} ${abs(day_pnl):,.2f}", fill=pnl_color, font=font_sub)
    draw.line((40, 210, 360, 210), fill=LINE, width=1)

    # Metrics
    draw.text((40, 240), "PERFORMANCE", fill=TEAL, font=font_label)
    metrics = [
        ("MTD:", stats.get("pnl_mtd", 0.0)),
        ("Last 30d:", stats.get("pnl_30d", 0.0)),
        ("Last 7d:", stats.get("pnl_7d", 0.0)),
    ]
    y = 280
    for label, val in metrics:
        val = float(val or 0.0)
        c = GREEN if val >= 0 else RED
        a = "▲" if val >= 0 else "▼"
        draw.text((40, y), label, fill=TEXT_COLOR, font=font_metrics)
        draw.text((250, y), f"{a} ${abs(val):,.2f}", fill=c, font=font_metrics)
        y += 40
    draw.line((40, 410, 360, 410), fill=LINE, width=1)

    # Streak dots
    draw.text((40, 440), "LAST 10 TRADING DAYS", fill=TEAL, font=font_label)
    dot_x, dot_y, dot_size = 45, 480, 25
    for is_win in (streak_map or [False] * 10):
        fill = GREEN if is_win else RED
        draw.ellipse([dot_x, dot_y, dot_x + dot_size, dot_y + dot_size], fill=fill)
        dot_x += 35

    os.makedirs(os.path.dirname(out_path), exist_ok=True) if os.path.dirname(out_path) else None
    img.save(out_path)
    return out_path


def generate_yearly_calendar_html(df: pd.DataFrame, year: int) -> str:
    """Recreate dexter's year performance map (green/red days by PnL) from a dexter-shaped df."""
    if df is None or df.empty:
        return f"<p>No performance data for {year}.</p>"

    if not isinstance(df["exit_date"].iloc[0], pd.Timestamp) or df["exit_date"].dt.tz is None:
        df = df.copy()
        df["exit_date"] = pd.to_datetime(df["exit_date"])
        if df["exit_date"].dt.tz is None:
            df["exit_date"] = df["exit_date"].dt.tz_localize("UTC").dt.tz_convert("America/New_York")

    df_year = df[df["exit_date"].dt.year == year]
    daily_pnl = df_year.groupby(df_year["exit_date"].dt.date)["pnl_dollar"].sum()

    html = f"""
    <style>
        body, .wp-site-blocks, .site-content, .entry-content, .main, #page {{
            background-color: #ffffff !important; color: #5d4037 !important;
        }}
        h2, h3 {{ color: #008080 !important; font-weight: 800 !important; }}
    </style>
    <div style="font-family: sans-serif; max-width: 1200px; margin: 0 auto; background-color:#ffffff; padding:40px; border-radius:15px;">
        <h2 style="text-align:center; color:#008080; margin-bottom: 30px; font-size:28px; font-weight:800;">{year} Performance Map</h2>
        <div style="display: flex; flex-wrap: wrap; justify-content: center; gap: 25px;">
    """
    cal = _calendar.Calendar(firstweekday=6)
    month_names = list(_calendar.month_name)[1:]

    for month_idx, month_name in enumerate(month_names, 1):
        html += f"""
        <div style="border: 1px solid #d7ccc8; border-radius: 12px; padding: 15px; background: #fafafa; width: 320px; box-shadow: 0 4px 12px rgba(0,0,0,0.03);">
            <h3 style="text-align: center; margin: 0 0 15px 0; font-size: 18px; color: #008080; font-weight:700;">{month_name}</h3>
            <table style="width: 100%; border-collapse: separate; border-spacing: 3px; table-layout: fixed;">
                <thead><tr style="font-size: 11px; color: #5d4037; font-weight:bold;"><th>S</th><th>M</th><th>T</th><th>W</th><th>T</th><th>F</th><th>S</th></tr></thead>
                <tbody>
        """
        for week in cal.monthdatescalendar(year, month_idx):
            html += "<tr>"
            for day in week:
                if day.month != month_idx:
                    html += "<td></td>"
                    continue
                pnl = daily_pnl.get(day, 0.0)
                if day in daily_pnl.index:
                    if pnl > 0:
                        bg, border, tc = "#e8f5e9", "#2e7d32", "#1b5e20"
                        pnl_text = f"+${pnl:,.0f}"
                    elif pnl < 0:
                        bg, border, tc = "#ffebee", "#c62828", "#b71c1c"
                        pnl_text = f"-${abs(pnl):,.0f}"
                    else:
                        bg, border, tc = "#fafafa", "#5d4037", "#5d4037"
                        pnl_text = "$0"
                    html += f"""
                    <td style="background-color:{bg}; border:1px solid {border}; color:{tc}; text-align:center; vertical-align:middle; border-radius:6px; height:45px; padding:2px; box-shadow:inset 0 1px 2px rgba(0,0,0,0.02); cursor:default;">
                        <div style="font-weight:bold; font-size:12px; margin-bottom:2px;">{day.day}</div>
                        <div style="font-size:9px; font-weight:600;">{pnl_text}</div>
                    </td>"""
                else:
                    html += f"""
                    <td style="background-color:#fafafa; color:#e0e0e0; text-align:center; vertical-align:top; border-radius:6px; height:45px; padding:5px; font-size:11px;">{day.day}</td>"""
            html += "</tr>"
        html += "</tbody></table></div>"
    html += "</div></div>"
    return html


# ---------------------------------------------------------------------------
# Convenience: render today's sidebar image from blog_stats output
# ---------------------------------------------------------------------------
def render_sidebar_image(round_trips: list[dict], today=None, out_dir: str = "reports") -> str:
    """Render the sidebar widget image from raw round-trips + today's PnL.

    Convenience that ties ``core.blog_stats`` (stats) to ``render_performance_card``.
    Returns the saved path.
    """
    import core.blog_stats as bs

    df = bs.round_trips_to_dexter_df(round_trips)
    today_ts = today or pd.Timestamp.now(tz="America/New_York").date()
    stats = bs.calculate_dashboard_stats(df, today=today_ts)
    streak = bs.get_last_10_days_performance(df, today=today_ts)
    today_pnl = 0.0
    if not df.empty:
        if df["exit_date"].dt.tz is None:
            df["exit_date"] = df["exit_date"].dt.tz_localize("UTC").dt.tz_convert("America/New_York")
        td = pd.Timestamp(today_ts, tz="America/New_York")
        today_pnl = float(df[df["exit_date"] <= td]["pnl_dollar"].sum())  # day's realized PnL

    os.makedirs(out_dir, exist_ok=True)
    return render_performance_card(stats, streak, today_pnl,
                                   os.path.join(out_dir, "sidebar_latest.png"),
                                   title_date=str(today_ts))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from core import feedback
    trips = feedback.compute_closed_round_trips()
    p = render_sidebar_image(trips)
    print("rendered:", p)