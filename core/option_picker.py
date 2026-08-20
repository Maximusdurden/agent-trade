"""Option selection engine for agent-trade.

Ports the proven scoring/selection logic from dexter-trader's
``utilities/option_picker.py`` but makes the DTE (days-to-expiry) window
configurable via ``core.config`` and overridable per-call (agent input).

Strategy: long calls & puts only (Level 2). Contract selection scores on
delta (0.25-0.75) and bid/ask spread (<= 50%), weighted 50/50.
"""

import re
import logging
from datetime import datetime, timedelta
from typing import Optional

from core import config

logger = logging.getLogger("OptionPicker")


class Contract:
    """Simple wrapper around the best-selected option contract."""
    def __init__(self, snapshot, info, delta):
        self.snapshot = snapshot
        self.symbol = format_option_symbol(info)
        self.strike_price = info["strike_price"]
        self.expiration_date = info["expiration_date"]
        self.underlying = info["root"]
        self.latest_quote = getattr(snapshot, "latest_quote", None)
        self.greeks = getattr(snapshot, "greeks", None)
        self.delta = delta
        self.dte = (self.expiration_date - datetime.now().date()).days
        self.open_interest = getattr(snapshot, "open_interest", 0)
        self.ask_price = float(getattr(self.latest_quote, "ask_price", 0) or 0)
        self.bid_price = float(getattr(self.latest_quote, "bid_price", 0) or 0)

    @property
    def premium_cost(self) -> float:
        """Total cost for one contract = ask * 100 (multiplier)."""
        return self.ask_price * 100.0

    @property
    def is_call(self) -> bool:
        # OCC: ROOT_(6) YYMMDD(6) C/P(1) strike(8) -> symbol[12] is C/P
        return self.symbol[12] == "C"

    def __repr__(self):
        return (f"Contract({self.underlying} {self.expiration_date} "
                f"{'C' if self.is_call else 'P'} ${self.strike_price:.2f} "
                f"ask={self.ask_price:.2f} delta={self.delta:.3f} dte={self.dte})")


def format_option_symbol(info) -> str:
    """Builds the 21-char padded OCC symbol from parsed info.

    ``info`` must have: ``root``, ``expiration_date`` (date), ``type``
    (CALL/PUT), ``strike_price`` (float). Also accepts a dict with those keys.
    """
    root = str(info["root"]).upper()
    expiration_date = info["expiration_date"]
    type_char = "C" if str(info["type"]).upper() == "CALL" else "P"
    strike_price = float(info["strike_price"])
    root_padded = f"{root:<6}"
    date_str = expiration_date.strftime("%y%m%d")
    strike_str = f"{int(strike_price * 1000):08d}"
    return f"{root_padded}{date_str}{type_char}{strike_str}"


def parse_option_symbol(symbol: str):
    """Parses an OCC option symbol string into a dict.

    Handles both space-separated ('AAPL 250117C00200000') and compact
    ('AAPL250117C00200000') forms. Returns:
        {root, expiration_date (date), type (CALL/PUT), strike_price, symbol}
    """
    clean = str(symbol or "").replace(" ", "").upper()
    match = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", clean)
    if not match:
        return None
    root, date_str, type_char, strike_str = match.groups()
    try:
        expiration_date = datetime.strptime(date_str, "%y%m%d").date()
    except ValueError:
        return None
    strike_price = float(strike_str) / 1000.0
    return {
        "root": root,
        "expiration_date": expiration_date,
        "type": "CALL" if type_char == "C" else "PUT",
        "strike_price": strike_price,
        "symbol": clean,
    }


def calculate_score(snapshot, current_stock_price: float, dte_min: int, dte_max: int) -> dict:
    """Scores an option contract snapshot on delta, spread, and DTE.

    Returns {'score': float, 'reason': str, 'delta': float}.
    Score -1 means invalid/not eligible.
    """
    latest_quote = getattr(snapshot, "latest_quote", None)
    greeks = getattr(snapshot, "greeks", None)

    if not latest_quote or not greeks:
        return {"score": -1, "reason": "Missing quote or greeks"}

    delta = getattr(greeks, "delta", None)
    if delta is None:
        return {"score": -1, "reason": "Missing delta"}
    delta = float(delta)
    if not (0.25 <= abs(delta) <= 0.75):
        return {"score": -1, "reason": f"Delta out of range ({delta:.2f})"}

    ask = float(getattr(latest_quote, "ask_price", 0) or 0)
    bid = float(getattr(latest_quote, "bid_price", 0) or 0)
    if ask <= 0:
        return {"score": -1, "reason": f"Invalid ask price (${ask})"}
    if bid <= 0.01:
        return {"score": -1, "reason": f"Invalid bid price (${bid})"}

    spread_percent = (ask - bid) / ask
    if spread_percent > 0.50:
        return {"score": -1, "reason": f"Spread too wide ({spread_percent:.1%})"}

    # DTE scoring: prefer contracts toward the middle of the allowed window
    dte = 0
    parsed = parse_option_symbol(getattr(snapshot, "symbol", ""))
    if parsed:
        dte = (parsed["expiration_date"] - datetime.now().date()).days
        dte = max(0, dte)
    if not (dte_min <= dte <= dte_max):
        return {"score": -1, "reason": f"DTE {dte} outside [{dte_min},{dte_max}]"}

    # Composite score: 50% spread tightness, 50% delta proximity to 0.5
    spread_score = (0.50 - spread_percent) / 0.50
    delta_score = (abs(delta) - 0.25) / 0.50
    total_score = (delta_score * 0.5) + (spread_score * 0.5)

    return {"score": total_score, "reason": "OK", "delta": delta}


def find_best_option(underlying_symbol: str, option_type: str,
                     days_out_min: Optional[int] = None,
                     days_out_max: Optional[int] = None,
                     otm_percent_min: Optional[float] = None,
                     otm_percent_max: Optional[float] = None,
                     current_stock_price: Optional[float] = None,
                     alpaca_client=None) -> Optional["Contract"]:
    """Finds the best option contract for an underlying.

    Args:
        underlying_symbol: The underlying ticker (e.g. 'NVDA').
        option_type: 'CALL' or 'PUT'.
        days_out_min/max: DTE window. Defaults to config.OPTIONS_DTE_MIN/MAX.
            The agent may override these (within config hard bounds enforced
            by guardrails).
        otm_percent_min/max: OTM% strike window. Defaults to config.
        current_stock_price: Optional pre-fetched price (avoids a second call).
        alpaca_client: The AlpacaClient instance. Required.

    Returns:
        The best ContractWrapper, or None if nothing suitable was found.
    """
    if alpaca_client is None:
        logger.error("find_best_option requires an alpaca_client.")
        return None

    days_out_min = days_out_min if days_out_min is not None else config.OPTIONS_DTE_MIN
    days_out_max = days_out_max if days_out_max is not None else config.OPTIONS_DTE_MAX
    otm_percent_min = otm_percent_min if otm_percent_min is not None else config.OPTIONS_OTM_PERCENT_MIN
    otm_percent_max = otm_percent_max if otm_percent_max is not None else config.OPTIONS_OTM_PERCENT_MAX
    opt_type = option_type.upper()

    # 1. Current underlying price
    if not current_stock_price:
        try:
            current_stock_price = alpaca_client.get_latest_price(underlying_symbol.upper())
        except Exception as e:
            logger.error(f"Failed to get latest price for {underlying_symbol}: {e}")
            return None
        if not current_stock_price:
            return None

    logger.info(f"Finding {opt_type} for {underlying_symbol} (Price: ${current_stock_price:.2f}, "
                f"DTE: {days_out_min}-{days_out_max})")

    # 2. Expiration range
    today = datetime.now().date()
    min_exp_date = (today + timedelta(days=days_out_min)).strftime("%Y-%m-%d")
    max_exp_date = (today + timedelta(days=days_out_max)).strftime("%Y-%m-%d")

    # 3. Strike range
    if opt_type == "CALL":
        min_strike = current_stock_price * (1 + otm_percent_min)
        max_strike = current_stock_price * (1 + otm_percent_max)
    elif opt_type == "PUT":
        min_strike = current_stock_price * (1 - otm_percent_max)
        max_strike = current_stock_price * (1 - otm_percent_min)
    else:
        logger.error(f"Invalid option_type: {option_type}")
        return None

    # 4. Fetch chain snapshot
    all_contracts = alpaca_client.get_option_chain_snapshot(
        underlying_symbol=underlying_symbol.upper(),
        expiration_date_gte=min_exp_date,
        expiration_date_lte=max_exp_date,
        strike_price_gte=min_strike,
        strike_price_lte=max_strike,
        contract_type=opt_type.lower(),
    )
    if not all_contracts:
        logger.warning(f"No option chain found for {underlying_symbol} in expiry {min_exp_date}..{max_exp_date}.")
        return None

    # 5. Score and select
    best_score = -1.0
    best_wrapper = None
    for symbol_key, snapshot in all_contracts.items():
        try:
            if isinstance(snapshot, str) or snapshot is None:
                continue
            if not hasattr(snapshot, "latest_quote") or not hasattr(snapshot, "greeks"):
                continue
            score_data = calculate_score(snapshot, current_stock_price, days_out_min, days_out_max)
            if "score" not in score_data or score_data["score"] <= best_score:
                continue
            parsed_info = parse_option_symbol(getattr(snapshot, "symbol", symbol_key))
            if not parsed_info:
                # Fall back to building from the chain key (may have spaces)
                parsed_info = parse_option_symbol(symbol_key)
                if not parsed_info:
                    continue
            best_score = score_data["score"]

            # Rebuild wrapper using the properly formatted OCC symbol
            occ = format_option_symbol(parsed_info)
            parsed_info["symbol"] = occ
            wrapper = Contract(snapshot, parsed_info, score_data.get("delta"))
            best_wrapper = wrapper
        except Exception as e:
            logger.error(f"Unexpected error scoring snapshot {symbol_key}: {e}", exc_info=True)

    if best_wrapper:
        logger.info(f"Selected best option for {underlying_symbol}: {best_wrapper} (Score: {best_score:.2f})")
    else:
        logger.warning(f"No suitable {opt_type} option found for {underlying_symbol} matching criteria.")
    return best_wrapper


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    from core.alpaca_client import AlpacaClient
    client = AlpacaClient()
    for tick in ["NVDA", "TSLA"]:
        print(find_best_option(tick, "CALL", alpaca_client=client))