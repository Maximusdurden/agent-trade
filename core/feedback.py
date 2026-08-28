"""Structured performance-feedback datastore for the MetaStrategist and Screener.

This module provides a single canonical source of truth for closed-trade
round-trips, decay-weighted per-symbol statistics, holding-time (whipsaw)
bucketing, and portfolio drawdown so both the Daily MetaStrategist and the
Screener can learn from actual outcomes instead of coarse aggregates.

The FIFO matching here is the canonical implementation.  The duplicated copies
in ``core/performance_auditor.py``, ``core/screener.get_symbol_win_rates`` and
``core/database.get_performance_summary`` are intentionally left in place for
backward compatibility / reporting, but any *new* strategy learning should read
from this module so the analytics stay consistent.
"""

import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from core.strategy_rules import normalize_symbol

logger = logging.getLogger("Feedback")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WHIPSAW_HOURS = 4            # any round-trip held under this is a "whipsaw"
DEFAULT_LOOKBACK_DAYS = 90   # decay-weighted window used by the strategist
DEFAULT_HALF_LIFE_DAYS = 30  # weighting half-life for decayed stats
MEMO_TTL_SECONDS = 60        # don't recompute FIFO more than once a minute

_BUCKETS = (
    "under_4h_whipsaw",
    "4h_to_1d",
    "1d_to_7d",
    "over_7d",
)

# ---------------------------------------------------------------------------
# Small TTL memo cache so a 15-min trading cycle doesn't re-run FIFO every tick
# ---------------------------------------------------------------------------
_memo: dict[tuple, tuple] = {}  # key -> (expires_at, value)


def _memoized(key, fn):
    now = time.monotonic()
    hit = _memo.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = fn()
    _memo[key] = (now + MEMO_TTL_SECONDS, value)
    return value


def _parse_ts(iso_str):
    """Parse an ISO timestamp to a naive UTC datetime (defensively)."""
    if not iso_str:
        return None
    s = str(iso_str)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def _sym(symbol: str) -> str:
    """Normalize a broker/config symbol to canonical upper form (e.g. SOL/USD)."""
    return normalize_symbol(symbol)


def next_strategy_version() -> str:
    """Return a timestamped rule-version id for tagging strategy_history rows.

    Lets future round-trip attribution score which rule version generated a given
    trade (e.g. ``v20260827-153012``).
    """
    return "v" + datetime.utcnow().strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# Round-trip computation
# ---------------------------------------------------------------------------
def compute_closed_round_trips(lookback_days: int | None = None) -> list[dict]:
    """FIFO-match buys->sells into closed round-trips.

    Each sell leg consumes the *oldest* still-open buys per symbol, producing a
    round-trip record tagged with holding duration, PnL, PnL% and a win flag.
    Partial matches yield partial round-trips (FIFO correctness preserved).

    Returns a list of dicts:
        {symbol, open_ts, close_ts, qty, entry_price, exit_price,
         pnl, pnl_pct, holding_hours, win}
    """
    from core import database

    rows = _memoized(
        ("trades",),
        lambda: _fetch_filled_trades(database),
    )

    round_trips = []
    buy_queues = defaultdict(list)  # symbol -> list of {qty, price, ts}
    cutoff = None
    if lookback_days:
        cutoff = datetime.utcnow() - timedelta(days=lookback_days)

    for r in rows:
        symbol = _sym(r["symbol"])
        side = (r["side"] or "").lower()
        qty = float(r["qty"] or 0.0)
        price = float(r["filled_avg_price"] or 0.0)
        ts = r["timestamp"]

        if side == "buy":
            buy_queues[symbol].append({"qty": qty, "price": price, "ts": ts})
        elif side == "sell":
            temp_qty = qty
            while temp_qty > 0 and buy_queues.get(symbol):
                b = buy_queues[symbol][0]
                matched = min(temp_qty, b["qty"])
                pnl = matched * (price - (b["price"] or 0.0))
                entry = b["price"] or 0.0
                pnl_pct = ((price - entry) / entry * 100.0) if entry else 0.0
                holding_hours = 0.0
                t_open = _parse_ts(b["ts"])
                t_close = _parse_ts(ts)
                if t_open is not None and t_close is not None:
                    holding_hours = (t_close - t_open).total_seconds() / 3600.0

                round_trips.append({
                    "symbol": symbol,
                    "open_ts": b["ts"],
                    "close_ts": ts,
                    "qty": matched,
                    "entry_price": entry,
                    "exit_price": price,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "holding_hours": holding_hours,
                    "win": pnl > 0,
                })
                temp_qty -= matched
                b["qty"] -= matched
                if b["qty"] <= 1e-9:
                    buy_queues[symbol].pop(0)

    if cutoff is not None:
        round_trips = [rt for rt in round_trips
                       if (t := _parse_ts(rt["close_ts"])) is not None and t >= cutoff]

    return round_trips


def today_realized_pnl() -> float:
    """Return the realized PnL from round-trips closed today (Eastern time).

    Used by the intra-day PnL circuit breaker. Round-trips are matched FIFO from
    the DB; only those whose close timestamp falls on today's Eastern date count.
    """
    trips = compute_closed_round_trips()
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except Exception:
        try:
            import pytz
            tz = pytz.timezone("America/New_York")
        except Exception:
            tz = None

    today = datetime.now(tz).date() if tz else datetime.now().date()
    total = 0.0
    for t in trips:
        close = _parse_ts(t["close_ts"])
        if close is None:
            continue
        if tz is not None:
            close_local = close.replace(tzinfo=timezone.utc).astimezone(tz)
            close_date = close_local.date()
        else:
            close_date = close.date()
        if close_date == today:
            total += t["pnl"]
    return total


def _fetch_filled_trades(database):
    with database.get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, timestamp, symbol, side, qty, filled_avg_price, status "
            "FROM trades WHERE status IN ('filled', 'partially_filled') ORDER BY id ASC"
        )
        return [dict(r) for r in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Decay weighting
# ---------------------------------------------------------------------------
def _age_weight(close_ts, half_life_days):
    """Exponential decay weight: 0.5 at ``half_life_days`` old, then halves."""
    t = _parse_ts(close_ts)
    if t is None:
        return 1.0
    age_days = (datetime.utcnow() - t).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def holding_bucket(hours: float) -> str:
    """Classify a round-trip holding duration into a whipsaw bucket label."""
    if hours < WHIPSAW_HOURS:
        return "under_4h_whipsaw"
    if hours < 24:
        return "4h_to_1d"
    if hours < 7 * 24:
        return "1d_to_7d"
    return "over_7d"


# ---------------------------------------------------------------------------
# Per-symbol + global stats
# ---------------------------------------------------------------------------
def symbol_stats(symbol: str | None = None, lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                 half_life_days: int = DEFAULT_HALF_LIFE_DAYS) -> dict:
    """Decay-weighted statistics filtered to one symbol (or all if ``symbol`` is None)."""
    trips = compute_closed_round_trips(lookback_days=lookback_days)
    if symbol is None:
        trips = trips
    else:
        wanted = _sym(symbol)
        trips = [t for t in trips if t["symbol"] == wanted]

    buckets = defaultdict(lambda: {"count": 0, "weight": 0.0, "pnl": 0.0})
    w_total = 0.0
    w_wins = 0.0
    w_pnl = 0.0
    gross_win = 0.0
    gross_loss = 0.0
    max_loss = 0.0
    max_win = 0.0
    w_holding_hours = 0.0

    for t in trips:
        w = _age_weight(t["close_ts"], half_life_days)
        w_total += w
        w_pnl += w * t["pnl"]
        if t["win"]:
            w_wins += w
            gross_win += w * t["pnl"]
            max_win = max(max_win, t["pnl"])
        else:
            gross_loss += w * abs(t["pnl"])
            max_loss = min(max_loss, t["pnl"])
        w_holding_hours += w * t["holding_hours"]
        bk = holding_bucket(t["holding_hours"])
        buckets[bk]["count"] += 1
        buckets[bk]["weight"] += w
        buckets[bk]["pnl"] += w * t["pnl"]

    win_rate = (w_wins / w_total * 100.0) if w_total > 0 else 0.0
    avg_pnl = (w_pnl / w_total) if w_total > 0 else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    avg_holding_hours = (w_holding_hours / w_total) if w_total > 0 else 0.0

    # Whipsaw share of *weighted* activity.
    whipsaw_weight = buckets["under_4h_whipsaw"]["weight"]
    whipsaw_ratio = (whipsaw_weight / w_total) if w_total > 0 else 0.0

    raw_count = len(trips)

    return {
        "symbol": _sym(symbol) if symbol else "ALL",
        "n_trades": raw_count,
        "decayed_n_trades": round(w_total, 2),
        "win_rate": round(win_rate, 2),
        "avg_pnl": round(avg_pnl, 2),
        "expectancy": round(w_pnl, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
        "max_win": round(max_win, 2),
        "max_loss": round(max_loss, 2),
        "avg_holding_hours": round(avg_holding_hours, 2),
        "whipsaw_ratio": round(whipsaw_ratio, 2),
        "buckets": {
            bk: {"count": buckets[bk]["count"],
                 "weight": round(buckets[bk]["weight"], 2),
                 "pnl": round(buckets[bk]["pnl"], 2)}
            for bk in _BUCKETS
        },
    }


def portfolio_stats(lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                    half_life_days: int = DEFAULT_HALF_LIFE_DAYS) -> dict:
    """Global portfolio metrics: total round-trip PnL + max drawdown from history."""
    from core import database

    trips = compute_closed_round_trips(lookback_days=lookback_days)
    total_pnl = sum(t["pnl"] for t in trips)

    peak_equity = 0.0
    max_drawdown_pct = 0.0
    current_equity = 0.0
    try:
        with database.get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT timestamp, equity, cash FROM portfolio_history ORDER BY timestamp ASC"
            )
            rows = [dict(r) for r in cursor.fetchall()]
        for r in rows:
            eq = float(r.get("equity") or 0.0)
            current_equity = eq
            if eq > peak_equity:
                peak_equity = eq
            if peak_equity > 0:
                dd = (peak_equity - eq) / peak_equity
                if dd > max_drawdown_pct:
                    max_drawdown_pct = dd
    except Exception as e:
        logger.warning(f"Could not read portfolio history for drawdown: {e}")

    return {
        "total_realized_pnl": round(total_pnl, 2),
        "peak_equity": round(peak_equity, 2),
        "current_equity": round(current_equity, 2),
        "max_drawdown_pct": round(max_drawdown_pct * 100.0, 2),
    }


# ---------------------------------------------------------------------------
# Text formatters for the MetaStrategist prompt
# ---------------------------------------------------------------------------
_BUCKET_LABELS = {
    "under_4h_whipsaw": "<4h (whipsaw)",
    "4h_to_1d": "4h-1d",
    "1d_to_7d": "1-7d",
    "over_7d": ">7d",
}


def format_symbol_feedback(stats: dict) -> str:
    """Human-readable, strategy-actionable summary for one symbol."""
    bk = stats.get("buckets", {})
    bucket_lines = ", ".join(
        f"{_BUCKET_LABELS[b]}: {bk[b]['count']} trades / ${bk[b]['pnl']:+,.2f} (decayed)"
        for b in _BUCKETS if bk.get(b, {}).get("count")
    ) or "no closed trades"

    return (
        f"- Symbol: {stats['symbol']}\n"
        f"- Closed round-trips (decayed window): {stats['n_trades']}\n"
        f"- Decay-weighted win rate: {stats['win_rate']}%\n"
        f"- Profit factor: {stats['profit_factor']}\n"
        f"- Expectancy (decayed net PnL): ${stats['expectancy']:+,.2f}\n"
        f"- Avg PnL / trade: ${stats['avg_pnl']:+,.2f}\n"
        f"- Largest win: ${stats['max_win']:+,.2f} | Largest loss: ${stats['max_loss']:+,.2f}\n"
        f"- Avg holding time: {stats['avg_holding_hours']:.1f}h | Whipsaw share: {stats['whipsaw_ratio']*100:.0f}%\n"
        f"- Holding-time buckets: {bucket_lines}"
    )


def feedback_text(symbol: str | None = None,
                  lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                  half_life_days: int = DEFAULT_HALF_LIFE_DAYS) -> str:
    """Full feedback block for a ticker (per-symbol + global context) for prompts."""
    st = symbol_stats(symbol, lookback_days=lookback_days, half_life_days=half_life_days)
    glob = portfolio_stats(lookback_days=lookback_days, half_life_days=half_life_days)

    lines = ["=== STRUCTURED PERFORMANCE FEEDBACK (LEARNING ENGINE) ==="]

    if symbol:
        lines.append(f"--- {st['symbol']} (decayed over past {lookback_days}d) ---")
        lines.append(format_symbol_feedback(st))
    lines.append("--- PORTFOLIO-GLOBAL CONTEXT ---")

    lines.append(
        f"- Total Realized PnL (window): ${glob['total_realized_pnl']:+,.2f}\n"
        f"- Peak Equity: ${glob['peak_equity']:,.2f} | Current Equity: ${glob['current_equity']:,.2f}\n"
        f"- Max Historical Drawdown: {glob['max_drawdown_pct']}%\n"
    )

    # Cognitive guidance for rule authors (kept actionable for the LLM).
    lines.append(
        "COGNITIVE LESSONS FOR RULE ADAPTATION:\n"
        "1. If the <4h (whipsaw) bucket dominates, the rule should add a faster "
        "regime filter / require stronger intraday-breakout confirmation, and "
        "should NOT simply restate yesterday's rule.\n"
        "2. If profit factor < 1.0 or expectancy is negative, be more "
        "conservative: smaller starter size, tighter entry thresholds, require "
        "confluences of support.\n"
        "3. The new rule MUST cite at least one concrete, guardrail-adjustable "
        "knob (intraday VWAP threshold, RSI entry band, max allocation %, or "
        "holding-time exit) so it is testable against future round-trips."
    )
    return "\n".join(lines)