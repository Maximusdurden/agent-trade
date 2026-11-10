"""Regenerate the 2026-11-08 forecast post as it would have been written then.

The original post was destroyed by a bad write in post_publish. This rebuilds it
with two hard constraints:

1. **No lookahead.** Every number comes from bars that closed on or before
   2026-11-06 (the last session before the post). The post was written Sunday
   evening 11/08, so nothing after Friday's close may influence it. Bars are
   fetched with an explicit ``end`` so the API cannot hand back later data.

2. **The honeypot link goes back in.** That post was the only inbound link to
   the bestiary, and it was the reason the link existed at all.

Run with --dry first. It prints the full body and the per-ticker calls.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import config, wordpress as wp  # noqa: E402

# --- theme colours (the child theme swaps --ink in dark mode) ---------------
INK = "var(--ink, #221d16)"
MUTED = "var(--muted, #6f6557)"
SURFACE = "var(--surface, #faf6ec)"
BORDER = "var(--border, #d8cdb8)"
GAIN = "var(--gain, #2e6b44)"
LOSS = "var(--loss, #b23b2e)"
ACCENT = "var(--accent, #c8432c)"

WATCH = ["SPY", "QQQ", "TSLA", "AMD", "NVDA", "AAPL", "MSFT", "META", "GOOGL", "AMZN"]

HONEYPOT_URL = "https://ai-honeypot-dc6gtgwbrq-ue.a.run.app/bestiary"

# The post was written Sunday 2026-11-08 evening. The last completed session was
# Friday 2026-11-06. Nothing after that close may be used.
AS_OF = datetime(2026, 11, 6, 21, 0, tzinfo=timezone.utc)  # Fri 16:00 ET close
PUBLISH_DATE = date(2026, 11, 8)
FORECAST_DATE = date(2026, 11, 9)  # Monday


def _technicals_as_of(client, symbol: str) -> dict | None:
    """Technical snapshot using ONLY bars that closed on or before AS_OF."""
    try:
        start = AS_OF - timedelta(days=140)
        df = client.get_historical_bars(
            symbol, limit=60, timeframe_str="day", start=start, end=AS_OF
        )
        if df is None or df.empty:
            return None
        df = df.tail(60)
        if len(df) < 20:
            return None
        close = df["close"]
        last = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        sma20 = float(close.tail(20).mean())
        sma50 = float(close.tail(min(50, len(close))).mean())

        delta = close.diff()
        up = delta.clip(lower=0).tail(14).mean()
        down = (-delta.clip(upper=0)).tail(14).mean()
        rsi = 100.0 - (100.0 / (1.0 + up / down)) if down else 100.0

        hi20 = float(close.tail(20).max())
        lo20 = float(close.tail(20).min())
        pos = (last - lo20) / (hi20 - lo20) * 100 if hi20 > lo20 else 50.0

        return {
            "symbol": symbol,
            "last": last,
            "chg_pct": (last / prev - 1.0) * 100.0,
            "sma20": sma20,
            "sma50": sma50,
            "rsi": rsi,
            "hi20": hi20,
            "lo20": lo20,
            "range_pos": pos,
            "bars": len(df),
            "last_bar": str(df.index[-1])[:10],
        }
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {symbol}: {exc}")
        return None


def _call(t: dict) -> tuple[str, str, str]:
    """Same mechanical rules as publish_forecast_post._call."""
    rsi, pos, chg = t["rsi"], t["range_pos"], t["chg_pct"]
    above20 = t["last"] > t["sma20"]
    above50 = t["last"] > t["sma50"]

    if rsi < 30 and pos < 25:
        return ("UP", "high",
                f"RSI {rsi:.0f} is deeply oversold and price sits in the bottom "
                f"{pos:.0f}% of its 20-day range")
    if rsi < 45 and chg > 5:
        return ("DOWN", "medium",
                f"it already ripped {chg:+.1f}% in one session off oversold levels, "
                f"so some of that gets given back")
    if rsi < 45 and not above20:
        return ("UP", "medium",
                f"RSI {rsi:.0f} is washed out and price is still under its 20-day "
                f"average, leaving room to snap back")
    if above20 and above50:
        return ("UP", "medium",
                "it is holding above both its 20- and 50-day averages while the rest "
                "of the tape is broken")
    if pos > 60:
        return ("FLAT", "low",
                f"it already sits in the top {100 - pos:.0f}% of its range with the "
                f"20-day average overhead")
    return ("FLAT", "low", "the signals are mixed and there is no clean edge either way")


def _intro(snaps: list[dict]) -> str:
    spy = next((s for s in snaps if s["symbol"] == "SPY"), None)
    qqq = next((s for s in snaps if s["symbol"] == "QQQ"), None)
    spy_txt = (
        f"SPY closed Friday at ${spy['last']:,.2f}, which is {spy['chg_pct']:+.2f}% on "
        f"the session and {((spy['last'] / spy['sma20']) - 1) * 100:+.1f}% versus its "
        f"20-day average"
        if spy else "SPY data was unavailable"
    )
    qqq_txt = (
        f"QQQ finished at ${qqq['last']:,.2f} with an RSI of {qqq['rsi']:.0f}"
        if qqq else "QQQ data was unavailable"
    )
    return (
        f"Sunday, November 8, 2026. The pack is rested, the bowls are stacked, and "
        f"the tape is a mess. Last week the market took a genuine bite out of itself: "
        f"Thursday was a proper ambush, with the broad tape gapping down and closing "
        f"on the lows, and Friday was the nervous sniff-around afterward. "
        f"{spy_txt}. {qqq_txt}."
        f"<br><br>"
        f"So what happens Monday? Here is my honest read, ticker by ticker, with the "
        f"evidence attached. I am not going to pretend I know. I am going to tell you "
        f"what the numbers are whispering and how much I trust the whisper."
        f"<br><br>"
        f"One thing worth naming: a lot of the selling last week was blamed on AI "
        f"nerves rather than on earnings. Which is funny, because the things doing the "
        f"selling and the reading are increasingly not people. I have been keeping an "
        f"eye on <a href='{HONEYPOT_URL}' style='color:{ACCENT}; font-weight:bold;'>"
        f"a running field guide to the crawlers themselves</a> &mdash; what they are, "
        f"what they claim to be, and what they actually do when nobody is watching. "
        f"Turns out quite a few of them lie about their names. I find that deeply "
        f"relatable."
    )


def _ticker_block(t: dict, direction: str, confidence: str, reason: str) -> str:
    arrow = {"UP": "&#9650;", "DOWN": "&#9660;", "FLAT": "&#9654;"}[direction]
    color = {"UP": GAIN, "DOWN": LOSS, "FLAT": MUTED}[direction]
    word = {"UP": "higher", "DOWN": "lower", "FLAT": "roughly flat"}[direction]
    return (
        f"<p style='margin:22px 0 4px 0;'>"
        f"<strong style='font-size:19px; color:{INK}; letter-spacing:0.02em;'>"
        f"{t['symbol']}</strong> "
        f"<span style='color:{color}; font-weight:bold;'>{arrow} {direction}</span> "
        f"<span style='color:{MUTED}; font-size:13px;'>({confidence} confidence)</span>"
        f"</p>"
        f"<p style='color:{INK}; line-height:1.7; font-size:16px; margin-top:0;'>"
        f"I expect {t['symbol']} to trade {word} on Monday. It closed at "
        f"${t['last']:,.2f} ({t['chg_pct']:+.2f}% on Friday), with an RSI of "
        f"{t['rsi']:.0f} and sitting {t['range_pos']:.0f}% of the way up its 20-day "
        f"range. The reason: {reason}."
        f"</p>"
    )


def _outro(snaps: list[dict]) -> str:
    ups = sum(1 for s in snaps if s["_dir"] == "UP")
    downs = sum(1 for s in snaps if s["_dir"] == "DOWN")
    flats = sum(1 for s in snaps if s["_dir"] == "FLAT")
    return (
        f"<p style='color:{INK}; line-height:1.7; font-size:17px;'>"
        f"Tally for Monday: {ups} up, {downs} down, {flats} flat. That is a bounce "
        f"lean, not a victory lap. The trend is still broken &mdash; almost everything "
        f"on this list is below its 20-day average &mdash; so treat any green as a "
        f"relief rally inside a damaged tape, not as the all-clear. If the bounce fails "
        f"early and the indices take out Thursday's low, I will be wrong in public and "
        f"I will say so."
        f"<br><br>"
        f"The honest summary: I am leaning up because the selling got extreme, not "
        f"because anything got fixed. Those are different things, and the market "
        f"usually figures out the difference by Wednesday."
        f"</p>"
    )


def build(dry: bool) -> int:
    from core.alpaca_client import AlpacaClient

    client = AlpacaClient()
    snaps: list[dict] = []
    print(f"Fetching bars with end={AS_OF.isoformat()} (no lookahead)\n")
    for sym in WATCH:
        t = _technicals_as_of(client, sym)
        if not t:
            print(f"  - {sym}: no data")
            continue
        d, c, r = _call(t)
        t["_dir"], t["_conf"], t["_reason"] = d, c, r
        snaps.append(t)
        print(
            f"  {sym:<6} {d:<5} ({c:<6}) ${t['last']:>9,.2f}  "
            f"RSI {t['rsi']:>5.1f}  range {t['range_pos']:>3.0f}%  "
            f"last_bar={t['last_bar']}"
        )

    if not snaps:
        print("\nNo data. Aborting.")
        return 1

    title = "Monday's Reckoning: Reading the Tape Before the Bell"
    body = (
        '<section class="wp-block-group" style="max-width:1200px;margin:0 auto;">'
        f'<div style="background:{SURFACE};border-radius:15px;padding:20px;'
        f'border:1px solid {BORDER};">'
        f"<h1 style='color:{INK};'>{title}</h1>"
        f"<p style='color:{INK}; font-size:17px; line-height:1.7;'>"
        f"{_intro(snaps)}</p>"
    )
    for t in snaps:
        body += _ticker_block(t, t["_dir"], t["_conf"], t["_reason"])
    body += _outro(snaps)
    body += (
        f"<p style='text-align:center;'><a href='{config.WP_URL}/trading-performance/' "
        f"style='color:{ACCENT}; font-weight:bold;'>[CALENDAR] View Performance</a></p>"
    )
    body += wp.get_disclaimer_html()
    body += "</div></section>"

    print(f"\nbody: {len(body)} chars")
    print(f"honeypot links: {body.count('a.run.app')}")

    if dry:
        out = PROJECT_ROOT / "_forecast_preview.html"
        out.write_text(body, encoding="utf-8")
        print(f"wrote {out}")
        return 0

    post_data = {
        "title": title,
        "content": body,
        "status": "publish",
        "date": f"{PUBLISH_DATE.isoformat()}T16:00:00",
        "excerpt": (
            "Dexter's ticker-by-ticker read on where SPY, QQQ, TSLA, AMD, NVDA, "
            "AAPL, MSFT, META, GOOGL and AMZN go on Monday."
        ),
        "_tickers": [s["symbol"] for s in snaps],
    }
    resp = wp.update_post(3252, post_data)
    if resp.status_code == 200:
        print(f"UPDATED: {resp.json().get('link')}")
        return 0
    print(f"FAILED {resp.status_code}: {resp.text[:300]}")
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry", action="store_true")
    args = p.parse_args()
    return build(args.dry)


if __name__ == "__main__":
    raise SystemExit(main())
