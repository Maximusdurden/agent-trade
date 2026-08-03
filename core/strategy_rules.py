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


def validate_strategy_rule(ticker: str, rule: str) -> tuple[bool, str]:
    """Validate that a persisted strategy rule is usable for its ticker.

    Key checks:
    - Rule is not empty and not the "no active strategy" placeholder.
    - For crypto tickers, the rule must reference the asset itself rather
      than only referencing SPY/QQQ (which would make the rule inoperable
      during 24/7 crypto-only windows).
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

    return True, "valid"