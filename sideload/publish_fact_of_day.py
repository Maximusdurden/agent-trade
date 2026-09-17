#!/usr/bin/env python3
"""Dexter's AMD Fact of the Day — daily blog post in Dexter's voice.

Publishes a daily WordPress post (in Dexter's persona) summarizing what the AMD
learning agent discovered today, so the user can keep up with the learning
without digging into the dashboard. It also pulls fresh AMD news for context.

Reuses the existing blog layer:
  - core/brain._apply_persona  -> Dexter voice styling
  - core/wordpress.publish_post -> WordPress REST publisher
  - core/discord_notifier      -> Discord notification
  - AlpacaClient.get_news      -> fresh AMD news for context

BRANDING: the persona must never reference internal systems. The post is framed
as "what Dexter learned about AMD today" — readers only see Treat Motivated
Capital + Dexter's voice.

Usage:
    python -m sideload.publish_fact_of_day            # publish today's post
    python -m sideload.publish_fact_of_day --dry      # print, don't publish
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload import config_sideload as sl_cfg
from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core import brain, wordpress as wp
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("FactOfDay")

VALIDATED_PATH = os.path.join(PROJECT_ROOT, "sideload", "backtest_validated.json")


def _load_learning_summary() -> dict:
    """Load the latest learning summary (from learn_amd output or validated json)."""
    summary = {
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "symbol": sl_cfg.SL_SYMBOL,
        "learned": "The AMD lane is still studying. No validated edge yet.",
        "best_config": None,
    }
    if os.path.exists(VALIDATED_PATH):
        try:
            with open(VALIDATED_PATH) as f:
                validated = json.load(f)
            if validated:
                best = validated[0]
                summary["best_config"] = best
                summary["learned"] = (
                    f"Best AMD setup: {best.get('interval')} bars, buy on RSI pullback "
                    f"to support (RSI <= {best.get('rsi_entry_max')}), out-of-sample "
                    f"expectancy ${best.get('test_expectancy', 0.0):.2f}/trade at "
                    f"{best.get('test_win_rate', 0.0):.0%} win rate over "
                    f"{best.get('test_trades', 0)} test trades."
                )
        except Exception as e:
            logger.warning(f"Could not load validated configs: {e}")
    return summary


def _fetch_amd_news(client: AlpacaClient) -> str:
    """Fetch fresh AMD news for context in the post."""
    try:
        news = client.get_news(sl_cfg.SL_SYMBOL, limit=3)
        if not news:
            return "No significant AMD news today."
        lines = []
        for n in news[:3]:
            headline = n.get("headline", "")
            summary = n.get("summary", "")
            lines.append(f"- {headline}: {summary}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Could not fetch AMD news: {e}")
        log_exception_to_jira(e, "AMD Fact-of-Day News Fetch Failure")
        return "No significant AMD news today."


def build_post_content(summary: dict, news_str: str) -> str:
    """Build the raw content and task instruction for the persona LLM."""
    raw = (
        f"Today's AMD learning ({summary['date']}):\n"
        f"{summary['learned']}\n\n"
        f"Fresh AMD news for context:\n{news_str}"
    )
    task = (
        "Write a short 'Dexter's AMD Fact of the Day' blog post in your voice. "
        "Explain, in plain dog terms, one interesting thing the AMD trading "
        "study learned today. Keep it to 120-180 words. Do NOT mention any "
        "internal systems, code, or how the numbers are produced. Frame it as "
        "what you (Dexter) noticed about AMD today. No markdown bolding. "
        "Include 1 sparse typo. End with a single-sentence takeaway. "
        "OUTPUT FORMAT: Respond with PLAIN TEXT ONLY — no JSON, no code fences, "
        "no labels. Just the blog post body as a clean paragraph or two."
    )
    content = brain._apply_persona(raw, task)
    return _unwrap_json(content)


def _unwrap_json(text: str) -> str:
    """Defensively unwrap a JSON envelope if the LLM returns one.

    Some models wrap plain-text output in ``{"output": "..."}``,
    ``{"blog_post": "..."}``, etc. If present, extract the inner string so we
    never publish raw JSON to WordPress. Handles ANY single-string-value dict
    (not just known keys), plus nested ``{"data": {"x": "..."}}`` shapes.
    """
    if not text:
        return text
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            import json as _json
            parsed = _json.loads(stripped)
            if isinstance(parsed, dict):
                # Known keys first.
                for key in ("output", "response", "content", "text", "body",
                            "message", "blog_post", "post", "article"):
                    val = parsed.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
                # Fallback: any single string value in the dict.
                str_vals = [v for v in parsed.values() if isinstance(v, str) and v.strip()]
                if len(str_vals) == 1:
                    return str_vals[0].strip()
                # Nested {"data": {"x": "..."}} shape.
                for v in parsed.values():
                    if isinstance(v, dict):
                        nested = _unwrap_json(json.dumps(v))
                        if nested != json.dumps(v):
                            return nested
        except Exception:
            pass
    return text


def _wrap_html(title: str, body: str, tickers: list[str] | None = None) -> str:
    """Wrap the fact-of-day in the same HTML shell as the daily market posts.

    Uses a DARK, readable text color (#2c2c2c) on the white card so the post is
    easy to read. Applies external hotlinks (AMD -> Yahoo Finance, indicators
    -> Investopedia) to the body before wrapping.
    """
    header = ('<section class="wp-block-group" style="max-width:1200px;margin:0 auto;">'
              '<div style="background:#ffffff;border-radius:15px;padding:20px;'
              'border:1px solid #d7ccc8;">')
    footer = "</div></section>"
    # Add external hotlinks (AMD -> Yahoo Finance, RSI/MACD/etc -> Investopedia).
    linked = wp.autolink_external(body, tickers=tickers)
    body_html = linked.replace("\n", "<br>")
    return (f"{header}<h1 style='color:#1a1a1a;'>{title}</h1>"
            f"<p style='color:#2c2c2c; font-size:17px;'>{body_html}</p>{footer}")


def publish(dry: bool = False) -> dict:
    """Build and publish today's AMD Fact of the Day. Returns a result dict."""
    summary = _load_learning_summary()
    client = AlpacaClient()
    news_str = _fetch_amd_news(client)
    content = build_post_content(summary, news_str)

    title = f"Dexter's AMD Fact of the Day — {summary['date']}"
    # Wrap in the same HTML shell as the daily market posts so the fact-of-day
    # matches the site's existing post format. Dark readable text + external
    # hotlinks (AMD -> Yahoo Finance, indicators -> Investopedia).
    html_content = _wrap_html(title, content, tickers=[sl_cfg.SL_SYMBOL])
    if dry:
        logger.info(f"[DRY] Title: {title}")
        logger.info(f"[DRY] Content:\n{html_content}")
        return {"status": "dry", "title": title, "content": html_content}

    post_data = {
        "title": title,
        "content": html_content,
        "status": "publish",
        # NOTE: do NOT set "date" here. Passing a naive utcnow() makes WordPress
        # treat it as a future GMT date and SCHEDULE the post (status "future")
        # instead of publishing it immediately. Omitting "date" lets WordPress
        # stamp the current time and publish right away.
    }
    resp = wp.publish_post(post_data)
    if resp.status_code in (200, 201):
        link = resp.json().get("link", "")
        logger.info(f"Published: {title} -> {link}")
        try:
            send_discord_message(f"**Dexter's AMD Fact of the Day**\n{title}\n{link}")
        except Exception as e:
            logger.warning(f"Discord notify failed: {e}")
        return {"status": "published", "title": title, "link": link}
    logger.error(f"Publish failed: {resp.status_code} {resp.text[:300]}")
    return {"status": "failed", "title": title, "error": resp.text[:300]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Dexter's AMD Fact of the Day")
    parser.add_argument("--dry", action="store_true", help="Print without publishing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    setup_jira_logging(app_name="agent-trade-sideload-factofday")
    try:
        result = publish(dry=args.dry)
        logger.info("Result: %s", json.dumps(result, indent=2, default=str))
    except Exception as e:
        logger.critical(f"Fact-of-day publish failed: {e}")
        log_exception_to_jira(e, "AMD Fact-of-Day Publish Failure")
        raise


if __name__ == "__main__":
    main()