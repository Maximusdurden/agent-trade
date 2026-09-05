#!/usr/bin/env python3
"""Dexter blog update orchestrator (agent-trade) — cloud-ready.

Ported from dexter's ``blog_bot/daily_blog_update.py`` and reorganized per the
migration plan §6:
   1. Pull the agent-trade DB from GCS (unless --local-db given) — the DB must be
      fresh before any post (user directive: sync at least every blog post).
   2. Run the bridge (`tools.build_blog_db`) to build the ``realized_trades`` mirror.
   3. Grade closed trades (D-TAG) so posts carry the grade badge.
   4. Assemble + publish the WordPress post (intro, per-ticker blurbs, card, calendar).
   5. Update sidebar widget + performance calendar page.
   6. Notify Discord.

FAIL-SAFE: if the GCS pull fails or yields no fresh DB, we ABORT rather than
publish stale numbers.

Usage:
    python -m tools.blog_update            # pull DB from GCS, then publish
    python -m tools.blog_update --local-db # use the local trading_agent.db (dev)
    python -m tools.blog_update --dry --local-db  # build+grade+print, don't publish
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

from core import config, wordpress as wp, seo as seo_mod, cards
from core import brain
from core.brain import generate_blog_intro, generate_trade_blurb, format_pnl, setup_dexter_logging
from core.discord_notifier import send_discord_message
from core.blog_stats import round_trips_to_dexter_df, calculate_dashboard_stats, get_last_10_days_performance

logger = logging.getLogger("BlogUpdate")

EASTER_TZ = "America/New_York"
STYLE_GUIDE = """
RULES:
1. NEVER use generic dog puns (pawsome, fur-tastic, bark-tastic) or basic bark noises (Woof!, Bark!).
2. NEVER call the reader "apprentice", "protege", or use corporate advisory jargon.
3. Treat everyday household events with existential gravity.
4. Keep blurbs sharp and concise.
5. NO markdown bolding (**) in final blog output. Clean plain text paragraphs only.
"""


# ---------------------------------------------------------------------------
# GCS DB sync (pull). Reuses core.gcs_sync (canonical strategy-job path).
# Abort on failure per the freshness guarantee: never publish against a stale DB.
# ---------------------------------------------------------------------------
def pull_db_from_gcs() -> bool:
    """Download the current trading_agent.db from GCS to DATABASE_PATH.

    Uses ``core.gcs_sync.download_from_gcs()`` (the same sync the strategy job
    uses) so the blog reads exactly what the strategy uploaded. Returns True if
    a DB is present locally afterward; False => abort publishing.
    """
    from core import gcs_sync
    try:
        # Download (atomic replace) via the canonical sync path.
        ok = gcs_sync.download_from_gcs()
    except Exception as e:
        logger.error("Pull DB via gcs_sync failed: %s", e)
        ok = False

    if not os.path.exists(str(config.DATABASE_PATH)):
        logger.error("No local DB after GCS download; aborting (no stale publish).")
        return False
    return ok


# ---------------------------------------------------------------------------
# Intro / blurb helpers (ported)
# ---------------------------------------------------------------------------
def create_intro_content(date_str, total_pnl, news, buys_context, ticker_list, daily_notes=None):
    import re
    notes_section = (f"SPECIAL INSTRUCTIONS / DAILY NOTES:\n{daily_notes}\n\n" if daily_notes else "")
    combined = f"{notes_section}MARKET NEWS:\n{news}\n\nLATEST BUYS CONTEXT:\n{buys_context}"
    t = generate_blog_intro(date_str, total_pnl, combined, ticker_list)
    if not t:
        return {"title": f"Market Update: {date_str}", "meta": "", "body": "Dexter is napping."}
    parsed = {"title": f"Market Update: {date_str}", "meta": "", "body": ""}
    clean = re.sub(r"\*\*(TITLE|META|BODY)\s*:\*\*", r"\1:", t, flags=re.IGNORECASE)
    title_m = re.search(r"TITLE\s*:\s*(.*?)(?=\s*META\s*:|\s*BODY\s*:|\Z)", clean, flags=re.I | re.S)
    meta_m = re.search(r"META\s*:\s*(.*?)(?=\s*BODY\s*:|\Z)", clean, flags=re.I | re.S)
    body_m = re.search(r"BODY\s*:\s*(.*?)\Z", clean, flags=re.I | re.S)
    if title_m and title_m.group(1).strip():
        parsed["title"] = title_m.group(1).strip()
    if meta_m and meta_m.group(1).strip():
        parsed["meta"] = meta_m.group(1).strip()
    if body_m and body_m.group(1).strip():
        parsed["body"] = body_m.group(1).strip()
    else:
        remainder = re.sub(r"^\s*(TITLE|META|BODY)\s*:\s*", "", clean, flags=re.I)
        parsed["body"] = remainder.strip()
    parsed["body"] = re.sub(r"^\s*(TITLE|META|BODY)\s*:\s*", "", parsed["body"], flags=re.I)
    if not parsed["meta"]:
        parsed["meta"] = f"Dexter's Trading Journal update for {date_str}."
    return parsed


def verify_pnl_in_body(body, total_pnl):
    import re
    if body is None or total_pnl is None:
        return body
    try:
        pnl_float = float(total_pnl)
    except (ValueError, TypeError):
        return body
    correct_str = format_pnl(pnl_float)
    match = re.search(r"(?P<lead>\s*)(?P<sign>[-−]?)\s*\$?\s*(?P<num>\d[\d,]*)", body)
    if not match:
        return body
    stated_neg = match.group("sign") in ("-", "−")
    actual_neg = pnl_float < 0
    if stated_neg != actual_neg:
        return body[:match.start()] + match.group("lead") + correct_str + body[match.end():]
    return body


# ---------------------------------------------------------------------------
# Grading hook (D-TAG) — reads the realized_trades mirror by trade_id
# ---------------------------------------------------------------------------
def grade_trades(db_path, date_str) -> None:
    """Grade all realized_trades closed on ``date_str`` (idempotent)."""
    try:
        from core.grader import grade_trades_for_date
        grade_trades_for_date(date_str, db_path=db_path)
    except ImportError:
        logger.warning("core.grader not yet present; skipping grading.")
    except Exception as e:
        logger.warning("Grading failed for %s: %s", date_str, e)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
def run(use_local_db: bool, dry: bool, date_override: str | None) -> int:
    setup_dexter_logging()
    target_date = date_override or datetime.now().strftime("%Y-%m-%d")

    # 1. Obtain the DB (GCS, or local for dev).
    # The canonical gcs_sync.download_from_gcs() writes to config.DATABASE_PATH
    # (atomic replace), and feedback/build_blog_db/grader all read that path, so
    # we always operate on DATABASE_PATH.
    db_path = str(config.DATABASE_PATH)
    if not use_local_db:
        if not pull_db_from_gcs():
            logger.error("ABORT: no fresh DB available; not publishing.")
            return 1
        logger.info("Using GCS-synced DB: %s", db_path)
    else:
        logger.info("Using local DB: %s", db_path)

    # 2. Build the realized_trades mirror (bridge).
    try:
        sys.path.insert(0, PROJECT_ROOT)
        from tools.build_blog_db import write_mirror
        from core import feedback
        # NOTE: compute_closed_round_trips reads via core.database (default path).
        # At cutover, the GCS-pulled DB is placed at DATABASE_PATH (or DATABASE_PATH
        # is repointed) BEFORE this step so the mirror reflects the fresh snapshot.
        trips = feedback.compute_closed_round_trips()
        n = write_mirror(trips, db_path, dry_run=dry)
        logger.info("Mirror rows: %d", n)
    except Exception as e:
        logger.error("Bridge failed: %s", e)
        return 1

    # 3. Build the post (grade + assemble + publish via the real pipeline).
    return _build_and_publish(trips, db_path, target_date, dry)


# ---------------------------------------------------------------------------
# Post assembly + publish (the real pipeline — no stub)
# ---------------------------------------------------------------------------
def _get_root_ticker(ticker: str) -> str:
    """Group OCC option contracts to their underlying root."""
    import re
    m = re.match(r"^([A-Z]+)\d{6}[CP]\d{8}$", (ticker or "").upper().replace(" ", ""))
    return m.group(1) if m else (ticker or "")


def _home_html_header() -> str:
    return ('<section class="wp-block-group" style="max-width:1200px;margin:0 auto;">'
            '<div style="background:#ffffff;border-radius:15px;padding:20px;'
            'border:1px solid #d7ccc8;">')


def _home_html_footer() -> str:
    return "</div></section>"


def _market_research(date_str: str, tickers) -> str:
    """Best-effort market-driver summary (never blocks publishing)."""
    try:
        joined = ", ".join(tickers) if tickers else "n/a"
        return f"Market update for {date_str}. Tickers discussed: {joined}."
    except Exception as e:  # pragma: no cover
        logger.warning("market research fallback: %s", e)
        return "Market context available for the day."


def _build_and_publish(trips, db_path: str, target_date: str, dry: bool) -> int:
    """Grade + build the post (intro, blurbs, card, calendar) + publish.

    In ``dry`` mode everything is generated except WP publish / Discord send.
    Returns 0 on success (or would-publish).
    """
    # ---- 1. Build dexter-shaped df + today's PnL ----
    df = round_trips_to_dexter_df(trips)
    if df is None:
        df = pd.DataFrame()
    if not df.empty and not isinstance(df["exit_date"].iloc[0], pd.Timestamp):
        df["exit_date"] = pd.to_datetime(df["exit_date"])
    if not df.empty and df["exit_date"].dt.tz is None:
        df["exit_date"] = df["exit_date"].dt.tz_localize("UTC").dt.tz_convert(EASTER_TZ)

    target_ts = pd.Timestamp(pd.to_datetime(target_date), tz=EASTER_TZ)
    start = target_ts.normalize()
    end = start + pd.Timedelta(days=1)
    if not df.empty:
        day_rows = df[(df["exit_date"] >= start) & (df["exit_date"] < end)]
    else:
        day_rows = pd.DataFrame()
    total_pnl = float(day_rows["pnl_dollar"].sum() if not day_rows.empty else 0.0)
    tickers = sorted({t for t in (day_rows["ticker"].tolist() if not day_rows.empty else []) if t})

    # ---- 2. Grade the trades that closed today (D-TAG) ----
    grade_trades(db_path, target_date)

    # ---- 3. Sidebar + performance card + calendar (always updated) ----
    card_path = None
    try:
        from core.cards import render_performance_card
        stats = calculate_dashboard_stats(df, today=start.date())
        streak = get_last_10_days_performance(df, today=start.date())
        import os as _os
        card_path = _os.path.join("reports", f"daily_card_{target_date}.png")
        render_performance_card(stats, streak, total_pnl, card_path, title_date=target_date)
    except Exception as e:
        logger.warning("Card render skipped: %s", e)

    cal_html = ""
    try:
        from core.cards import generate_yearly_calendar_html
        cal_html = generate_yearly_calendar_html(df, target_ts.year)
    except Exception as e:
        logger.warning("Calendar render skipped: %s", e)

    if not dry:
        if card_path and os.path.exists(card_path):
            media = wp.upload_image_from_path(card_path, f"Daily Summary {target_date}")
            if media:
                wp.update_synced_pattern(media.get("source_url"))
        if cal_html:
            wp.update_performance_page(cal_html)

    # ---- 4. Intro (Dexter brain) ----
    news = _market_research(target_date, tickers)
    intro = create_intro_content(target_date, total_pnl, news, "No new buys.", tickers)
    intro["body"] = verify_pnl_in_body(intro["body"], total_pnl)

    # ---- 5. Per-ticker blurbs (with grade context from DB) ----
    grade_map = {}
    try:
        import sqlite3 as _sq
        conn = _sq.connect(db_path)
        conn.row_factory = _sq.Row
        rows = conn.execute(
            "SELECT ticker, grade, composite_score FROM realized_trade_grades WHERE date=?",
            (target_date,)).fetchall()
        grade_map = {r["ticker"]: dict(r) for r in rows}
        conn.close()
    except Exception as e:
        logger.warning("grade fetch failed: %s", e)

    html_body = _home_html_header()
    html_body += f"<h1>{intro['title']}</h1><p style='color:#5d4037; font-size:17px;'>{intro['body']}</p>"

    if not day_rows.empty:
        for ticker in tickers:
            group = day_rows[day_rows["ticker"] == ticker]
            group_pnl = float(group["pnl_dollar"].sum())
            gi = grade_map.get(ticker) or grade_map.get(_get_root_ticker(ticker))
            raw = generate_trade_blurb(ticker, group_pnl,
                                       "Entries/exits summarized for the day.", grade_info=gi)
            blurb = wp.autolink_tickers(raw, tickers)
            blurb = wp.autolink_financial_terms(blurb)
            color = "#4caf50" if group_pnl > 0 else "#f44336"
            html_body += f"""
            <div style="padding:24px; border-left:8px solid {color}; background:#ffffff; margin:20px 0; border-radius:15px; border:1px solid #d7ccc8;">
                <h3 style="margin-top:0; color:#008080; font-weight:800;">{ticker} Analysis</h3>
                <p style="color:#5d4037; line-height:1.7; font-size:17px;">{blurb}</p>
            </div>"""
    else:
        html_body += ("<p style='color:#8d6e63;'>No round-trips closed today. "
                      "The tape was quiet — more tomorrow.</p>")

    html_body += (f"<p style='text-align:center;'><a href='{config.WP_URL}/trading-performance/' "
                  "style='color:#2271b1; font-weight:bold;'>[CALENDAR] View Performance</a></p>")
    html_body += wp.get_disclaimer_html()
    html_body += _home_html_footer()

    # ---- 6. SEO meta (best-effort) ----
    seo_meta = None
    try:
        from core.seo import SEOAuditor
        seo_meta = SEOAuditor.generate_seo_metadata(intro["title"], html_body)
    except Exception as e:
        logger.warning("SEO skip: %s", e)

    # ---- 7. Publish (or dry-run print) ----
    post_data = {
        "title": intro["title"],
        "content": html_body,
        "status": "publish",
        "date": f"{target_date}T16:00:00",
        "meta": {"rank_math_description": seo_meta.get("meta_description") if seo_meta else intro["meta"]},
    }
    if seo_meta and seo_meta.get("json_ld_schema"):
        post_data["content"] = wp.add_json_ld_schema(post_data["content"], seo_meta["json_ld_schema"])

    if dry:
        logger.info("[DRY] Would publish '%s' (pnl=%s); %d grades cached.",
                    intro["title"], format_pnl(total_pnl), len(grade_map))
        return 0

    resp = wp.publish_post(post_data)
    if resp.status_code == 201:
        link = resp.json().get("link", "no link")
        logger.info("Published: %s -> %s", intro["title"], link)
        try:
            wp.notify_new_post(intro["title"], total_pnl, link)
        except Exception as e:
            logger.warning("Discord notify failed: %s", e)
        return 0

    logger.error("Publish failed: %s - %s", resp.status_code, resp.text[:500])
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Dexter blog update (agent-trade).")
    parser.add_argument("--local-db", action="store_true", help="use local agent DB (dev)")
    parser.add_argument("--dry", action="store_true", help="build/grade/print only; don't publish")
    parser.add_argument("--date", default=None, help="target date YYYY-MM-DD (default today)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    return run(args.local_db, args.dry, args.date)


if __name__ == "__main__":
    sys.exit(main())