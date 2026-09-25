#!/usr/bin/env python3
"""SPY/QQQ 0DTE sentiment + S/R anchor component (Phase 1).

Implements the "latch-on" strategy's data-ingestion layer:

  1. SENTIMENT SCORER (Option 1 — VADER via Alpaca News API)
     - Pulls Alpaca News API headlines for SPY/QQQ published between
       07:00:00 and 09:15:00 America/New_York for the current session.
     - Cleans headline text (strips source tags, author handles, boilerplate
       URLs).
     - Computes an NLTK VADER compound score per headline in [-1.0, 1.0].
     - Aggregate score = arithmetic mean of all headline compound scores.
     - Bias arming gate:
         Bullish armed : score >=  0.40
         Bearish armed : score <= -0.40
         Neutral       : -0.40 < score < 0.40  (no trade authorized)
     - Zero-news handling: score = 0.0 (disarmed). No multi-day fallback.

  2. S/R ANCHOR MAPPER (09:30 ET)
     - Static anchors: Prior Day High (PDH), Prior Day Low (PDL),
       Prior Day Close (PDC), Pre-market High (PMH), Pre-market Low (PML).
     - Dynamic anchor: Anchored VWAP starting from the 09:30 open bar.

Usage:
    python -m sideload.options_sentiment_sr --date 2026-09-25
    python -m sideload.options_sentiment_sr --date 2026-09-25 --no-discord
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

PROJECT_ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sideload.jira_logging import setup_jira_logging, log_exception_to_jira
from core.alpaca_client import AlpacaClient
from core.discord_notifier import send_discord_message

logger = logging.getLogger("OptionsSentimentSR")

OUT_DIR = os.path.join(PROJECT_ROOT, "sideload")
ET = ZoneInfo("America/New_York")

# Strategy universe (Phase 1: SPY/QQQ/IWM index funds).
TICKERS = ["SPY", "QQQ", "IWM"]

# Sentiment ingestion window (ET).
NEWS_START = dtime(7, 0, 0)
NEWS_END = dtime(9, 15, 0)

# Bias arming thresholds.
BULLISH_THRESHOLD = 0.40
BEARISH_THRESHOLD = -0.40

# Intraday bar interval for VWAP / PMH / PML.
INTRADAY_INTERVAL = "1min"
# Pre-market window for PMH/PML (ET).
PRE_MARKET_START = dtime(4, 0, 0)
PRE_MARKET_END = dtime(9, 29, 0)
# Anchored VWAP start (ET).
VWAP_START = dtime(9, 30, 0)


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------
def clean_headline(text: str) -> str:
    """Strip source tags, author handles, and boilerplate URLs from a headline.

    Examples removed:
      - "BREAKING: ..."  (kept, it's content)
      - "@Reuters ..."   (author handle)
      - "https://..."    (URLs)
      - "[Reuters] ..."  (source tag)
    """
    if not text:
        return ""
    t = text
    # Remove URLs.
    t = re.sub(r"https?://\S+", "", t)
    # Remove author handles (@handle).
    t = re.sub(r"@\w+", "", t)
    # Remove bracketed source tags like [Reuters], (Reuters).
    t = re.sub(r"[\[\(][A-Za-z0-9 .\-]{1,40}[\]\)]", "", t)
    # Remove leading source prefixes like "Reuters: " or "Reuters - ".
    t = re.sub(r"^[A-Za-z0-9 .\-]{1,40}:\s+", "", t)
    t = re.sub(r"^[A-Za-z0-9 .\-]{1,40}\s+-\s+", "", t)
    # Collapse whitespace.
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ---------------------------------------------------------------------------
# Sentiment scoring (VADER via Alpaca News API)
# ---------------------------------------------------------------------------
def _get_vader():
    """Return a configured VADER SentimentIntensityAnalyzer (downloads lexicon)."""
    import nltk
    from nltk.sentiment.vader import SentimentIntensityAnalyzer
    try:
        nltk.data.find("sentiment/vader_lexicon.zip")
    except LookupError:
        nltk.download("vader_lexicon", quiet=True)
    return SentimentIntensityAnalyzer()


def fetch_news_window(client: AlpacaClient, symbol: str, session_date: str) -> list[dict]:
    """Fetch Alpaca news for ``symbol`` published in the 07:00-09:15 ET window.

    Args:
        client: AlpacaClient (must have a live news_client).
        symbol: Ticker (SPY/QQQ).
        session_date: YYYY-MM-DD trading session date (ET).

    Returns:
        List of dicts: {headline, source, summary, url, published_et}.
    """
    if client.is_mock or not getattr(client, "news_client", None):
        logger.info(f"[MOCK] No live news client; returning empty news for {symbol}.")
        return []

    # Build the ET window for the session date.
    day = datetime.strptime(session_date, "%Y-%m-%d").date()
    start_et = datetime.combine(day, NEWS_START, tzinfo=ET)
    end_et = datetime.combine(day, NEWS_END, tzinfo=ET)
    # Alpaca expects UTC.
    start_utc = start_et.astimezone(ZoneInfo("UTC"))
    end_utc = end_et.astimezone(ZoneInfo("UTC"))

    try:
        from alpaca.data.requests import NewsRequest
        request_params = NewsRequest(
            symbols=symbol.upper(),
            start=start_utc,
            end=end_utc,
            limit=100,
        )
        news_response = client.news_client.get_news(request_params)
    except Exception as e:
        logger.warning(f"Failed to fetch news for {symbol} on {session_date}: {e}")
        return []

    articles = []
    # The NewsSet response wraps articles under .data["news"].
    news_items = []
    if hasattr(news_response, "data") and isinstance(news_response.data, dict):
        news_items = news_response.data.get("news", [])
    elif hasattr(news_response, "news"):
        news_items = getattr(news_response, "news", [])
    for item in news_items:
        headline = getattr(item, "headline", "") or ""
        source = getattr(item, "source", "") or ""
        summary = getattr(item, "summary", "") or getattr(item, "content", "")[:200]
        url = str(getattr(item, "url", "")) if getattr(item, "url", None) else ""
        # Timestamp: prefer created_at, fall back to updated_at.
        ts = getattr(item, "created_at", None) or getattr(item, "updated_at", None)
        published_et = None
        if ts is not None:
            try:
                published_et = pd.Timestamp(ts).tz_convert(ET)
            except Exception:
                published_et = None
        articles.append({
            "headline": headline,
            "source": source,
            "summary": summary,
            "url": url,
            "published_et": published_et,
        })
    return articles


def score_sentiment(client: AlpacaClient, symbol: str, session_date: str) -> dict:
    """Compute the VADER aggregate sentiment score for a symbol in the window.

    Returns a dict with score, armed bias, per-headline scores, and counts.
    """
    sia = _get_vader()
    articles = fetch_news_window(client, symbol, session_date)

    if not articles:
        return {
            "symbol": symbol,
            "date": session_date,
            "score": 0.0,
            "bias": "NEUTRAL",
            "armed": False,
            "headline_count": 0,
            "headlines": [],
        }

    scored = []
    for a in articles:
        cleaned = clean_headline(a["headline"])
        compound = sia.polarity_scores(cleaned)["compound"] if cleaned else 0.0
        scored.append({
            "headline": a["headline"],
            "cleaned": cleaned,
            "source": a["source"],
            "compound": round(compound, 4),
            "published_et": str(a["published_et"]) if a["published_et"] is not None else None,
        })

    aggregate = sum(s["compound"] for s in scored) / len(scored)

    if aggregate >= BULLISH_THRESHOLD:
        bias, armed = "BULLISH", True
    elif aggregate <= BEARISH_THRESHOLD:
        bias, armed = "BEARISH", True
    else:
        bias, armed = "NEUTRAL", False

    return {
        "symbol": symbol,
        "date": session_date,
        "score": round(aggregate, 4),
        "bias": bias,
        "armed": armed,
        "headline_count": len(scored),
        "headlines": scored,
    }


# ---------------------------------------------------------------------------
# S/R anchor mapper (09:30 ET)
# ---------------------------------------------------------------------------
def _to_et(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(ET)


def compute_sr_anchors(client: AlpacaClient, symbol: str, session_date: str) -> dict:
    """Compute PDH/PDL/PDC, PMH/PML, and anchored VWAP for a session date.

    Args:
        client: AlpacaClient.
        symbol: Ticker.
        session_date: YYYY-MM-DD (ET).

    Returns:
        Dict of anchor levels, or {} if insufficient data.
    """
    day = datetime.strptime(session_date, "%Y-%m-%d").date()

    # --- Prior day daily bars (for PDH/PDL/PDC) ---
    daily = client.get_historical_bars(symbol, limit=10, timeframe_str="day")
    if daily is None or daily.empty:
        logger.warning(f"No daily bars for {symbol}.")
        return {}
    if isinstance(daily.index, pd.MultiIndex):
        daily = daily.reset_index(level=0, drop=True)
    daily.index = pd.to_datetime(daily.index)
    daily = daily.sort_index()
    # Normalize the daily index to ET (naive) for consistent date comparison.
    if daily.index.tzinfo is not None:
        daily.index = daily.index.tz_convert(ET).tz_localize(None)
    # Find the last trading day strictly before the session date.
    prior = daily[daily.index < pd.Timestamp(day)]
    if prior.empty:
        logger.warning(f"No prior-day bars before {session_date} for {symbol}.")
        return {}
    prior_row = prior.iloc[-1]
    pdh = float(prior_row["high"])
    pdl = float(prior_row["low"])
    pdc = float(prior_row["close"])

    # --- Intraday bars for PMH/PML and anchored VWAP ---
    intraday = client.get_historical_bars_paginated(
        symbol, timeframe_str=INTRADAY_INTERVAL, days_back=5)
    if intraday is None or intraday.empty:
        logger.warning(f"No intraday bars for {symbol}.")
        return {}
    if isinstance(intraday.index, pd.MultiIndex):
        intraday = intraday.reset_index(level=0, drop=True)
    intraday.index = pd.to_datetime(intraday.index)
    if intraday.index.tzinfo is None:
        intraday.index = intraday.index.tz_localize("UTC")
    intraday.index = intraday.index.tz_convert(ET)
    intraday = intraday.sort_index()

    day_start = pd.Timestamp(day, tz=ET)
    day_end = day_start + timedelta(days=1)
    day_bars = intraday[(intraday.index >= day_start) & (intraday.index < day_end)]
    if day_bars.empty:
        logger.warning(f"No intraday bars on {session_date} for {symbol}.")
        return {}

    # Pre-market window (04:00 - 09:29 ET).
    pm_start = day_start.replace(hour=PRE_MARKET_START.hour, minute=PRE_MARKET_START.minute)
    pm_end = day_start.replace(hour=PRE_MARKET_END.hour, minute=PRE_MARKET_END.minute)
    pm_bars = day_bars[(day_bars.index >= pm_start) & (day_bars.index <= pm_end)]
    pmh = float(pm_bars["high"].max()) if not pm_bars.empty else None
    pml = float(pm_bars["low"].min()) if not pm_bars.empty else None

    # Anchored VWAP from 09:30 open bar.
    vwap_start = day_start.replace(hour=VWAP_START.hour, minute=VWAP_START.minute)
    vwap_bars = day_bars[day_bars.index >= vwap_start]
    vwap = None
    if not vwap_bars.empty and {"close", "volume"}.issubset(vwap_bars.columns):
        tp = (vwap_bars["high"] + vwap_bars["low"] + vwap_bars["close"]) / 3.0
        vwap = float((tp * vwap_bars["volume"]).sum() / vwap_bars["volume"].sum())

    return {
        "symbol": symbol,
        "date": session_date,
        "pdh": round(pdh, 2),
        "pdl": round(pdl, 2),
        "pdc": round(pdc, 2),
        "pmh": round(pmh, 2) if pmh is not None else None,
        "pml": round(pml, 2) if pml is not None else None,
        "anchored_vwap": round(vwap, 2) if vwap is not None else None,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(session_date: str, send_discord: bool = True) -> dict:
    """Run the sentiment + S/R pipeline for SPY/QQQ on a session date."""
    client = AlpacaClient()
    results = {"date": session_date, "tickers": {}}

    for sym in TICKERS:
        sentiment = score_sentiment(client, sym, session_date)
        anchors = compute_sr_anchors(client, sym, session_date)
        results["tickers"][sym] = {
            "sentiment": sentiment,
            "sr_anchors": anchors,
        }
        logger.info(
            f"[{sym}] sentiment={sentiment['score']} bias={sentiment['bias']} "
            f"armed={sentiment['armed']} headlines={sentiment['headline_count']}"
        )
        logger.info(f"[{sym}] anchors={anchors}")

    # Persist JSON.
    out_path = os.path.join(OUT_DIR, f"options_sentiment_sr_{session_date}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info(f"Wrote results to {out_path}")

    if send_discord:
        try:
            lines = [f"**Options Sentiment/SR — {session_date}**"]
            for sym, data in results["tickers"].items():
                s = data["sentiment"]
                a = data["sr_anchors"]
                lines.append(
                    f"`{sym}` score={s['score']} bias={s['bias']} armed={s['armed']} "
                    f"n={s['headline_count']} | PDH={a.get('pdh')} PDL={a.get('pdl')} "
                    f"PDC={a.get('pdc')} PMH={a.get('pmh')} PML={a.get('pml')} "
                    f"VWAP={a.get('anchored_vwap')}"
                )
            send_discord_message("\n".join(lines))
        except Exception as e:
            logger.warning(f"Discord notification failed (non-fatal): {e}")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="SPY/QQQ 0DTE sentiment + S/R anchors")
    parser.add_argument("--date", default=datetime.now(ET).strftime("%Y-%m-%d"),
                        help="Session date YYYY-MM-DD (ET). Default: today.")
    parser.add_argument("--no-discord", action="store_true",
                        help="Skip the Discord notification.")
    args = parser.parse_args()

    setup_jira_logging(app_name="agent-trade-sideload")
    try:
        results = run(args.date, send_discord=not args.no_discord)
        print(json.dumps(results, indent=2, default=str))
    except Exception as e:
        log_exception_to_jira(e, "options_sentiment_sr", {"date": args.date})
        logger.exception("options_sentiment_sr failed")
        raise


if __name__ == "__main__":
    main()