#!/usr/bin/env python3
"""WordPress client for the Treat Motivated Capital blog.

Ports dexter's ``blog_bot/daily_blog_update.py`` WordPress helpers into
agent-trade, reading settings from ``core.config`` (WP_* / BLOG_*) and posting
Discord notifications via ``core.discord_notifier``.

Surface:
    get_auth_header, get_retry_session, get_or_create_tag_id, autolink_tickers,
    autolink_financial_terms, get_disclaimer_html, upload_image_from_path,
    update_synced_pattern (sidebar), update_performance_page (calendar),
    get_latest_published_date, publish_post

BRANDING is preserved: links/titles reference only Treat Motivated Capital.
"""

from __future__ import annotations

import base64
import json
import re
import logging
import os
import sys
from datetime import datetime, date, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core import config
from core.discord_notifier import send_discord_message

logger = logging.getLogger("WordPress")

WP_URL = config.WP_URL
WP_USER = config.WP_USER
WP_APP_PASSWORD = config.WP_APP_PASSWORD
SIDEBAR_WIDGET_TITLE = config.WP_SIDEBAR_WIDGET_TITLE
PERFORMANCE_PAGE_TITLE = config.WP_PERFORMANCE_PAGE_TITLE

_CACHED_AUTH = None


def get_auth_header() -> dict:
    """WordPress Basic Auth header (username + application password)."""
    global _CACHED_AUTH
    if _CACHED_AUTH:
        return _CACHED_AUTH.copy()
    credentials = f"{WP_USER}:{WP_APP_PASSWORD}"
    token = base64.b64encode(credentials.encode())
    _CACHED_AUTH = {"Authorization": f"Basic {token.decode('utf-8')}"}
    return _CACHED_AUTH.copy()


def get_retry_session(retries=3, backoff_factor=0.3) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries, read=retries, connect=retries,
        backoff_factor=backoff_factor, status_forcelist=(500, 502, 503, 504),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_or_create_tag_id(tag_name: str) -> int | None:
    headers = get_auth_header()
    try:
        resp = requests.get(f"{WP_URL}/wp-json/wp/v2/tags?search={tag_name}", headers=headers)
        if resp.status_code == 200:
            for t in resp.json():
                if t["name"].upper() == tag_name.upper():
                    return t["id"]
        create_resp = get_retry_session().post(
            f"{WP_URL}/wp-json/wp/v2/tags", headers=headers, json={"name": tag_name})
        if create_resp.status_code == 201:
            return create_resp.json()["id"]
    except Exception as e:
        logger.warning("Tag error (%s): %s", tag_name, e)
    return None


def autolink_tickers(text: str, tickers_list) -> str:
    if not tickers_list or not text:
        return text
    sorted_tickers = sorted(tickers_list, key=len, reverse=True)
    for ticker in sorted_tickers:
        link = (f'<a href="https://treatmotivated.capital/tag/{ticker.lower()}/" '
                f'style="font-weight:bold; color:#008080; text-decoration:underline;">{ticker}</a>')
        pattern = re.compile(f"\\b{re.escape(ticker)}\\b(?![^<]*</a>)")
        text = pattern.sub(link, text)
    return text


def autolink_financial_terms(text: str) -> str:
    if not text:
        return text
    terms = {
        "EMA", "Exponential Moving Average", "SMA", "Simple Moving Average",
        "Golden Cross", "Death Cross", "Bearish Crossover", "Bullish Crossover",
        "Hard Stop", "Stop Loss", "Trailing Stop", "Limit Order", "Market Order",
        "Slippage", "Support", "Resistance", "RSI", "Relative Strength Index",
        "MACD", "Volume", "Candlestick", "Grid", "Grid Trading", "Going Long",
        "Shorting", "Short Sale", "PnL", "Profit and Loss", "Volatility",
        "Bull Market", "Bear Market", "Sector", "Sectors",
    }
    for term in sorted(terms, key=len, reverse=True):
        tag_slug = term.lower().replace(" ", "-")
        link = (f'<a href="https://treatmotivated.capital/tag/{tag_slug}/" '
                f'style="font-weight:bold; color:#008080; text-decoration:underline;">\\1</a>')
        pattern = re.compile(f"\\b({re.escape(term)}s?)\\b(?![^<]*</a>)", re.IGNORECASE)
        text = pattern.sub(link, text)
    return text


def get_disclaimer_html() -> str:
    return """
    <div class="wp-block-group" style="background-color:#fafafa; color:#777; padding:20px; font-size:13px; border-top:1px solid #eee; margin-top:40px; border-radius:8px; border:1px solid #eee;">
        <p style="margin:0;"><strong>Disclaimer:</strong> This blog is for <strong>educational and entertainment purposes only</strong>. The author (Dexter) is a dog.</p>
        <p style="margin:0;"><strong>Not Financial Advice:</strong> All results are Paper Trading (Simulated).</p>
    </div>
    """


def upload_image_from_path(full_path: str, title: str) -> dict | None:
    if not full_path or not os.path.exists(full_path):
        return None
    url = f"{WP_URL}/wp-json/wp/v2/media"
    headers = get_auth_header()
    headers["Content-Disposition"] = f"attachment; filename={os.path.basename(full_path)}"
    headers["Content-Type"] = "image/png"
    try:
        with open(full_path, "rb") as f:
            r = get_retry_session().post(url, headers=headers, data=f.read())
        return r.json() if r.status_code == 201 else None
    except Exception as e:
        logger.warning("Upload failed: %s", e)
        return None


def update_synced_pattern(image_url: str) -> bool:
    """Update the 'Dexter Sidebar Widget' synced pattern with a new image URL."""
    logger.info("Updating sidebar widget ('%s')...", SIDEBAR_WIDGET_TITLE)
    headers = get_auth_header()
    widget_id = None
    successful_endpoint = None

    for ep in ("wp/v2/wp_block", "wp/v2/blocks"):
        try:
            resp = requests.get(f"{WP_URL}/wp-json/{ep}?search={SIDEBAR_WIDGET_TITLE}", headers=headers)
            if resp.status_code != 200:
                continue
            for item in resp.json():
                title_raw = item.get("title", "")
                title_text = ""
                if isinstance(title_raw, dict):
                    title_text = title_raw.get("rendered", "") or title_raw.get("raw", "")
                elif isinstance(title_raw, str):
                    title_text = title_raw
                if title_text.strip().lower() == SIDEBAR_WIDGET_TITLE.lower():
                    widget_id = item["id"]
                    successful_endpoint = ep
                    break
            if widget_id:
                break
        except Exception as e:
            logger.warning("Sidebar search warning on %s: %s", ep, e)

    if not widget_id:
        logger.error("Could not find pattern '%s'.", SIDEBAR_WIDGET_TITLE)
        return False

    new_content = f"""
    <style>
        body, .wp-site-blocks, .site-content, .entry-content, .main, #page {{
            background-color: #ffffff !important; color: #5d4037 !important;
        }}
        h1, h2, h3, h4, h5, h6 {{ color: #008080 !important; font-weight: 800 !important; }}
        a {{ color: #008080 !important; text-decoration: underline !important; }}
        .dexter-sidebar-image {{
            border-radius:15px; box-shadow: 0 10px 40px rgba(0,128,128,0.15);
            width:100%; border: 1px solid #eee;
        }}
    </style>
    <div class="wp-block-group">
        <figure class="wp-block-image size-large">
            <img src="{image_url}" alt="Dexter Daily Stats" class="dexter-sidebar-image"/>
        </figure>
    </div>
    """
    update_url = f"{WP_URL}/wp-json/{successful_endpoint}/{widget_id}"
    try:
        update_resp = get_retry_session().post(update_url, headers=headers, json={"content": new_content})
        if update_resp.status_code == 200:
            logger.info("Sidebar widget updated (ID: %s)", widget_id)
            return True
        logger.error("Widget update failed: %s", update_resp.status_code)
        return False
    except Exception as e:
        logger.warning("Widget error: %s", e)
        return False


def update_performance_page(html_content: str) -> None:
    """Find or create the 'Trading Performance' page and update it."""
    headers = get_auth_header()
    page_id = None
    try:
        resp = requests.get(f"{WP_URL}/wp-json/wp/v2/pages?search={PERFORMANCE_PAGE_TITLE}", headers=headers)
        if resp.status_code == 200:
            for p in resp.json():
                if p["title"]["rendered"] == PERFORMANCE_PAGE_TITLE:
                    page_id = p["id"]
                    break
    except Exception as e:
        logger.warning("Page search error: %s", e)
        return

    data = {"title": PERFORMANCE_PAGE_TITLE, "content": html_content, "status": "publish"}
    url = f"{WP_URL}/wp-json/wp/v2/pages"
    if page_id:
        url += f"/{page_id}"
    try:
        r = get_retry_session().post(url, headers=headers, json=data)
        if r.status_code in (200, 201):
            logger.info("Calendar page updated (%s).", page_id or "created")
        else:
            logger.error("Page update failed: %s - %s", r.status_code, r.text)
    except Exception as e:
        logger.warning("Page request failed: %s", e)


def get_latest_published_date() -> date:
    try:
        resp = requests.get(
            f"{WP_URL}/wp-json/wp/v2/posts?per_page=1&status=publish", headers=get_auth_header())
        if resp.status_code == 200 and resp.json():
            return datetime.strptime(resp.json()[0]["date"].split("T")[0], "%Y-%m-%d").date()
    except Exception:
        pass
    return (datetime.now().date() - timedelta(days=1))


def publish_post(post_data: dict) -> requests.Response:
    """POST a post to WordPress (title/content/status/tags/featured_media...)."""
    return get_retry_session().post(
        f"{WP_URL}/wp-json/wp/v2/posts", headers=get_auth_header(), json=post_data, timeout=15)


def notify_new_post(title: str, pnl: float, link: str) -> None:
    """Send the blog-post notification to Discord."""
    pnl_str = f"-${abs(pnl):,.0f}" if pnl < 0 else f"${pnl:,.0f}"
    msg = f"**New Dexter Blog Post!**\n**Title:** {title}\n**PnL:** {pnl_str}\n**Link:** {link}"
    send_discord_message(msg)


def add_json_ld_schema(content: str, schema: dict) -> str:
    """Append an injected JSON-LD BlogPosting schema script to post content."""
    schema_json = json.dumps(schema, indent=2)
    return content + (
        "\n\n<!-- Dexter-Webmaster Schema Injection -->\n"
        f'<script type="application/ld+json">\n{schema_json}\n</script>\n'
    )