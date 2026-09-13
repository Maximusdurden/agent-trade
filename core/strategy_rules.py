"""Validation helpers for strategy rules used by the MetaStrategist and runner.

Decouples crypto/equity classification from the monolithic runner so that
``is_crypto_symbol`` can be imported without bringing in the whole trading
dependency graph.  Also provides ``validate_strategy_rule`` which checks
that a persisted rule is actually usable for its ticker — critical after
the strategist may have hallucinated SPY-centric conditions for a crypto
pair.
"""

import re


CRYPTO_QUOTES = {"USD", "USDT", "USDC", "BTC"}
KNOWN_CRYPTO_BASES = {
    "ADA", "AVAX", "BTC", "DOGE", "DOT", "ETH", "LINK", "LTC",
    "MATIC", "SHIB", "SOL", "UNI", "XRP",
}

# Rule-selectivity gate (Phase 5). A rule that is true "almost always" is a
# churn generator — the PG failure mode: "IF vwap_dist < +0.5% AND RSI < 65
# THEN buy" is true most of the time, so the brain churned 18 buys in 2 days
# before any round-trip closed. These thresholds flag rules whose entry
# conditions are so loose they'd fire on noise rather than a real setup.
#
# A VWAP-distance entry threshold smaller than this (in %) is inside the noise
# band (the KO lesson: a sub-1% VWAP blip is not a signal). The real-data edge
# (2026-09-13) showed winners enter on real pullback, not sub-1% blips.
MIN_VWAP_DIST_THRESHOLD_PCT = 1.0
# An RSI entry cap looser (higher) than this means "buy almost always" — RSI<65
# is true most of the time. Selective entries require a tighter RSI band.
MAX_RSI_ENTRY_CAP = 55.0


def build_symbol_to_cluster(clusters: dict) -> dict:
    """Flatten ``{cluster_name: [symbols]}`` into ``{SYMBOL: cluster_name}``.

    Symbols not present in any cluster are not included (callers treat them as
    singleton clusters). Symbols are normalized to canonical upper form.
    """
    mapping = {}
    for cluster_name, members in (clusters or {}).items():
        for sym in members or []:
            mapping[normalize_symbol(sym)] = cluster_name
    return mapping


def normalize_symbol(symbol: str) -> str:
    """Normalize broker and configuration symbols to an uppercase slash form.

    On Alpaca crypto symbols arrive without the slash (e.g. "SOLUSD") while
    the trading universe stores them with a slash (e.g. "SOL/USD"). This
    function bridges the two formats so comparisons work consistently.
    """
    normalized = (symbol or "").strip().upper().replace("-", "/")
    if "/" in normalized:
        return normalized
    for quote in sorted(CRYPTO_QUOTES, key=len, reverse=True):
        if normalized.endswith(quote) and normalized[:-len(quote)] in KNOWN_CRYPTO_BASES:
            return f"{normalized[:-len(quote)]}/{quote}"
    return normalized


def is_crypto_symbol(symbol: str) -> bool:
    """Return whether a symbol is a recognized crypto pair."""
    normalized = normalize_symbol(symbol)
    if "/" not in normalized:
        return False
    base, quote = normalized.split("/", 1)
    return bool(base) and quote in CRYPTO_QUOTES


def _rule_selectivity_reason(rule: str) -> str | None:
    """Return a reason if a rule's entry thresholds are too loose (churn-prone).

    Flags the PG failure mode: a rule whose BUY conditions are true almost
    always (e.g. "IF vwap_dist < +0.5% AND RSI < 65 THEN buy") generates churn
    because the brain re-enters constantly. We detect two permissive patterns:

    1. A VWAP-distance entry threshold smaller than MIN_VWAP_DIST_THRESHOLD_PCT
       (e.g. "< +0.5%") — inside the noise band, not a real signal.
    2. An RSI entry cap looser than MAX_RSI_ENTRY_CAP (e.g. "RSI < 65") — true
       most of the time.

    Returns None when the rule looks selective enough.
    """
    text = (rule or "").upper()
    if not text:
        return None

    # 1. VWAP-distance threshold too small (noise-band entry).
    #    Matches "vwap_dist < +0.5%", "vwap_dist_pct < 0.5", "below VWAP by <1%".
    vwap_m = re.search(
        r"VWAP[_\s]*DIST[_\s]*PCT?\s*(?:IS\s*)?(?:LESS\s+THAN|<|BELOW)\s*[+]?(\d+(?:\.\d+)?)\s*%",
        text,
    )
    if vwap_m:
        try:
            if float(vwap_m.group(1)) < MIN_VWAP_DIST_THRESHOLD_PCT:
                return (
                    f"rule_not_selective_vwap_dist_{vwap_m.group(1)}pct"
                )
        except ValueError:
            pass

    # 2. RSI entry cap too loose (buy-almost-always).
    #    Matches "RSI < 65", "RSI below 65", "RSI is under 65".
    rsi_m = re.search(
        r"RSI\s*(?:IS\s*)?(?:BELOW|UNDER|LESS\s+THAN|<)\s*(\d+(?:\.\d+)?)",
        text,
    )
    if rsi_m:
        try:
            if float(rsi_m.group(1)) > MAX_RSI_ENTRY_CAP:
                return f"rule_not_selective_rsi_cap_{rsi_m.group(1)}"
        except ValueError:
            pass

    return None


def validate_strategy_rule(ticker: str, rule: str) -> tuple[bool, str]:
    """Validate that a persisted strategy rule is usable for its ticker.

    Key checks:
    - Rule is not empty and not the "no active strategy" placeholder.
    - For crypto tickers, the rule must reference the asset itself rather
      than only referencing SPY/QQQ (which would make the rule inoperable
      during 24/7 crypto-only windows).
    - Rule selectivity: entry thresholds must not be so loose they'd fire on
      noise (the PG "buy whenever" churn failure mode).
    """
    normalized_ticker = normalize_symbol(ticker)
    text = (rule or "").strip()
    if not text or text.startswith("No active strategy rules defined for "):
        return False, "missing_rule"

    if is_crypto_symbol(normalized_ticker):
        upper_rule = text.upper()
        base = normalized_ticker.split("/", 1)[0]
        mentions_target = bool(re.search(rf"\b{re.escape(base)}\b", upper_rule))
        mentions_equity_indices = bool(re.search(r"\b(?:SPY|QQQ)\b", upper_rule))
        if mentions_equity_indices and not mentions_target:
            return False, "crypto_rule_scoped_to_equity_indices"

    # Rule-selectivity gate: reject rules whose entry thresholds are so loose
    # they'd churn (PG failure mode). Applies to equities (crypto has bracket
    # TP/SL and different noise characteristics).
    if not is_crypto_symbol(normalized_ticker):
        sel_reason = _rule_selectivity_reason(text)
        if sel_reason:
            return False, sel_reason

    return True, "valid"