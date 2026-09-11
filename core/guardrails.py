import logging
import re
import math
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from core import config
from core.strategy_rules import normalize_symbol, build_symbol_to_cluster

logger = logging.getLogger("Guardrails")

# OCC option symbol detection: 6-char root (padded), 6-digit date, C/P, 8-digit strike
OCC_SYMBOL_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$", re.IGNORECASE)


def is_occ_symbol(symbol: str) -> bool:
    """Return True if a symbol is an OCC option contract."""
    clean = (symbol or "").upper().replace(" ", "")
    return bool(OCC_SYMBOL_RE.match(clean)) and not clean.endswith("USD")


def is_crypto_member(symbol: str) -> bool:
    """Return True if a normalized symbol is a crypto quote (contains '/')."""
    return "/" in normalize_symbol(symbol)


class RiskGuardrails:
    """Deterministic security/risk layer between LLM decisions and execution."""
    
    def __init__(self):
        # Build the flattened symbol -> cluster mapping once from config.
        self.symbol_to_cluster = build_symbol_to_cluster(
            getattr(config, "CORRELATION_CLUSTERS", {})
        )

    def _cluster_of(self, symbol: str) -> str:
        """Return the correlation cluster name for a symbol, else its own singleton."""
        sym = normalize_symbol(symbol)
        return self.symbol_to_cluster.get(sym, sym)

    def _cluster_exposure(self, symbol: str, proposed_value: float,
                          current_positions: dict) -> tuple[str, float]:
        """Return (cluster_name, current total dollar exposure incl. proposed buy).

        Sums the market value of every held position in the same cluster as
        ``symbol`` plus the proposed buy's dollar value. Positions not in the
        cluster mapping are ignored (they belong to their own singleton cluster).
        """
        cluster = self._cluster_of(symbol)
        exposure = proposed_value
        for pos_symbol, pos in current_positions.items():
            if self._cluster_of(pos_symbol) == cluster:
                exposure += float(pos.get("market_value", 0.0) or 0.0)
        return cluster, exposure

    def _in_latest_watchlist(self, symbol: str) -> bool:
        """Return True if ``symbol`` is in the latest screener watchlist.

        Uses the same normalization the universe check uses (raw and slash-stripped
        forms). A symbol in the latest watchlist is actively endorsed by the
        screener and is always eligible for a BUY.
        """
        try:
            from core import database
            latest = database.get_latest_watchlist_raw()
        except Exception:
            latest = []
        sym = symbol.upper().replace("/", "")
        for ws in latest:
            ws_upper = (ws or "").upper()
            if ws_upper == symbol.upper() or ws_upper.replace("/", "") == sym:
                return True
        return False

    def _universe_guardrail_reason(self, symbol: str, current_positions: dict) -> str | None:
        """Return a rejection reason if a NEW BUY to ``symbol`` should be blocked by
        the strict-universe guardrail.

        When STRICT_UNIVERSE_ENABLED, a BUY to a symbol that is NOT in the latest
        screener watchlist AND NOT currently held is blocked. This prevents the
        fallback path and the static TRADING_UNIVERSE from opening NEW positions in
        names the screener never endorsed (e.g. SPY/QQQ/TSLA/MS in the data), which
        dominated equity losses (-$540).

        Held positions are exempt so the agent can always manage (SELL) or top-up an
        existing position. Averaging-down on a held position is separately guarded by
        the anti-scale-in guardrail. Crypto is exempt (it is the profitable book).
        """
        if not getattr(config, "STRICT_UNIVERSE_ENABLED", True):
            return None
        sym = normalize_symbol(symbol)
        if is_crypto_member(sym) or "/" in sym:
            return None  # crypto is 24/7 and evidence shows it's the profitable book
        if self._in_latest_watchlist(symbol):
            return None  # screener-endorsed this cycle -> always eligible
        held = current_positions.get(symbol) or current_positions.get(symbol.replace("/", ""))
        if isinstance(held, dict) and float(held.get("qty", 0.0) or 0.0) > 0:
            return None  # held position -> must be manageable (SELL or top-up)
        try:
            from core import database
            wl = database.get_latest_watchlist_raw()
        except Exception:
            wl = []
        return (f"Rejected: Strict-universe guardrail. {symbol} is not in the latest "
                f"watchlist {wl}) and is not currently held. New BUYs are only "
                f"allowed on screener-endorsed symbols. Use SELL/HOLD to manage any "
                f"existing position in this name.")

    def _anti_scale_in_reason(self, symbol: str, proposed_qty: float,
                              current_price: float, current_positions: dict) -> str | None:
        """Return a rejection reason if a BUY would average DOWN into a held position.

        The MS loss pattern (-$226) was "buy-the-dip scale-in": the agent bought the
        same name repeatedly as it sagged, accumulating a falling knife and then
        capitulating at the low. This guard blocks ADDING to a held position when the
        current price is BELOW the position's average entry price. SELLs are never
        blocked. Crypto is exempt (it is the profitable book and scales differently).

        **Screener-endorsed exemption:** a symbol that is in the *latest* screener
        watchlist is actively endorsed and is always eligible for a BUY (see
        ``_universe_guardrail_reason``), so it is NOT subject to this guardrail.
        This lets a high-conviction add to a name the screener is currently
        endorsing go through, even slightly below entry — matching the documented
        intent of blocking only *unendorsed* averaging-down (the MS failure mode).

        **Noise tolerance:** trivial dips within ``ANTI_SCALE_IN_TOLERANCE_PCT``
        (default 0.5%) are treated as flat — not "averaging down into a loser."
        This prevents a 0.1% quote dip from wrongly blocking a legitimate add
        (e.g. PG held at $147.54, currently $147.42 = 0.08% below entry).
        """
        if proposed_qty <= 0 or current_price <= 0:
            return None
        if is_crypto_member(symbol) or "/" in symbol:
            return None
        # Screener-endorsed symbols are actively sanctioned -> allow the add.
        if self._in_latest_watchlist(symbol):
            return None
        pos = current_positions.get(symbol)
        if not isinstance(pos, dict):
            return None
        avg_entry = float(pos.get("avg_entry_price", 0.0) or 0.0)
        if avg_entry <= 0:
            return None
        # Only guard "averaging DOWN" (current price below avg entry).
        if current_price >= avg_entry:
            return None
        drawdown_pct = (avg_entry - current_price) / avg_entry * 100.0
        # Noise tolerance: treat sub-tolerance dips as flat, not a losing add.
        tolerance = float(getattr(config, "ANTI_SCALE_IN_TOLERANCE_PCT", 0.5))
        if drawdown_pct <= tolerance:
            return None
        return (f"Rejected: Anti-scale-in guardrail. {symbol} is held at ${avg_entry:.2f} "
                f"but currently ${current_price:.2f} ({drawdown_pct:.1f}% below entry). "
                f"Adding to a losing position is the MS dip-add failure mode. "
                f"Use SELL/HOLD to de-risk instead of averaging down.")

    def _circuit_breaker_reason(self, symbol: str) -> str | None:
        """Return a rejection reason if a BUY to ``symbol`` should be blocked.

        Uses the symbol's closed round-trips (from core.feedback) to detect:
        1. Consecutive losses: the most recent ``MAX_CONSECUTIVE_LOSSES`` closed
           round-trips are all losses -> the strategy keeps re-entering a loser.
        2. Whipsaw trap: >= ``MIN_WHIPSAW_TRADES`` closed trades AND the share of
           <4h round-trips exceeds ``MAX_WHIPSAW_RATIO`` -> the symbol whipsaws.

        Returns ``None`` when the symbol is clear to buy.
        """
        try:
            from core.feedback import compute_closed_round_trips
            lookback = getattr(config, "CIRCUIT_BREAKER_LOOKBACK_DAYS", 90)
            trips = compute_closed_round_trips(lookback_days=lookback)
            sym_trips = [t for t in trips if t["symbol"] == normalize_symbol(symbol)]
            if not sym_trips:
                return None

            # 1. Consecutive-loss circuit breaker.
            max_losses = int(getattr(config, "MAX_CONSECUTIVE_LOSSES", 3))
            # Round-trips are appended in chronological order; take the tail.
            recent = sym_trips[-max_losses:]
            if len(recent) >= max_losses and all(not t["win"] for t in recent):
                total = sum(t["pnl"] for t in recent)
                return (f"Rejected: Per-ticker circuit breaker. {symbol} has "
                        f"{max_losses} consecutive losing round-trips "
                        f"(recent PnL ${total:+,.2f}). BUY blocked to stop re-entering a losing name.")

            # 2. Whipsaw-trap circuit breaker.
            max_ratio = float(getattr(config, "MAX_WHIPSAW_RATIO", 0.60))
            min_trades = int(getattr(config, "MIN_WHIPSAW_TRADES", 4))
            if len(sym_trips) >= min_trades:
                whipsaw = sum(1 for t in sym_trips if t["holding_hours"] < 4.0)
                ratio = whipsaw / len(sym_trips)
                if ratio >= max_ratio:
                    return (f"Rejected: Whipsaw-trap circuit breaker. {symbol} has "
                            f"{whipsaw}/{len(sym_trips)} round-trips under 4h "
                            f"({ratio*100:.0f}% >= {max_ratio*100:.0f}%). BUY blocked.")

            # 3. Low win-rate circuit breaker: catch chronic losers (e.g. KO at
            # 0%, MS at 17%) that bleed on net even without 3 *consecutive* losses.
            min_low_win_trades = int(getattr(config, "MIN_LOW_WIN_RATE_TRADES", 5))
            max_low_win_rate = float(getattr(config, "MAX_LOW_WIN_RATE", 0.25))
            if len(sym_trips) >= min_low_win_trades:
                wins = sum(1 for t in sym_trips if t["win"])
                win_rate = wins / len(sym_trips)
                if win_rate < max_low_win_rate:
                    total = sum(t["pnl"] for t in sym_trips)
                    return (f"Rejected: Low win-rate circuit breaker. {symbol} has "
                            f"{wins}/{len(sym_trips)} winning round-trips "
                            f"({win_rate*100:.0f}% < {max_low_win_rate*100:.0f}%) "
                            f"with net PnL ${total:+,.2f}. BUY blocked to stop "
                            f"re-entering a chronic loser.")
        except Exception as err:
            logger.error(f"Error checking circuit breaker for {symbol}: {err}")
        return None

    def _intraday_pnl_breaker_reason(self, account_state: dict) -> str | None:
        """Return a rejection reason if the day's PnL loss exceeds the intra-day limit.

        Combines today's realized PnL (from the DB, FIFO) with the current
        unrealized PnL (from the account). If the total intra-day loss exceeds
        INTRADAY_LOSS_LIMIT_PCT of equity, block new BUYs (SELLs still allowed).
        """
        if not getattr(config, "INTRADAY_BREAKER_ENABLED", True):
            return None
        try:
            from core.feedback import today_realized_pnl
            equity = float(account_state.get("equity", 0.0) or 0.0)
            if equity <= 0:
                return None
            realized = today_realized_pnl()
            unrealized = float(account_state.get("unrealized_pnl", 0.0) or 0.0)
            intraday_pnl = realized + unrealized
            limit_pct = float(getattr(config, "INTRADAY_LOSS_LIMIT_PCT", 0.04))
            loss_pct = -intraday_pnl / equity
            if loss_pct >= limit_pct:
                return (f"Rejected: Intra-day PnL circuit breaker. Day PnL "
                        f"${intraday_pnl:+,.2f} (realized ${realized:+,.2f} + "
                        f"unrealized ${unrealized:+,.2f}) is a {loss_pct:.2%} loss "
                        f">= {limit_pct:.2%} limit. BUY blocked to stop the bleeding.")
        except Exception as err:
            logger.error(f"Error checking intra-day PnL breaker: {err}")
        return None

    def is_market_open_check(self) -> tuple[bool, str]:
        """
        Checks if the US Equity market (NYSE/NASDAQ) is currently open.
        Regular trading hours are Monday through Friday, 9:30 AM to 4:00 PM Eastern Time.
        """
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("America/New_York")
            now = datetime.now(tz)
        except Exception:
            try:
                import pytz
                tz = pytz.timezone("America/New_York")
                now = datetime.now(tz)
            except Exception:
                # Fallback: Assume current local machine time is in Eastern Time
                logger.warning("Could not load zoneinfo or pytz. Falling back to local time zone for market hours check.")
                now = datetime.now()
                
        # Check day of week (0 = Monday, ..., 6 = Sunday)
        weekday = now.weekday()
        if weekday >= 5:
            return False, f"Market closed: Weekend ({now.strftime('%A')})."
            
        # Check hour and minute
        market_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_end = now.replace(hour=16, minute=0, second=0, microsecond=0)
        
        if now < market_start:
            return False, f"Market closed: Pre-market (Current Eastern Time: {now.strftime('%H:%M')})."
        elif now > market_end:
            return False, f"Market closed: Post-market (Current Eastern Time: {now.strftime('%H:%M')})."
            
        return True, "Market is open."

    # ------------------------------------------------------------------
    # WHIPSAW-PREVENTION GUARDRAILS (2026-09-09)
    # ------------------------------------------------------------------

    def _vwap_dead_zone_reason(self, symbol: str, action: str, current_price: float,
                               indicators: dict | None) -> str | None:
        """Return a rejection reason if a BUY/SELL is inside the VWAP dead zone.

        Fix 1: the brain is told to "buy below VWAP, sell above VWAP" with no
        dead zone, so any cross of VWAP flips the decision (WFC churned a ~$0.50
        band around VWAP). This deterministic guardrail blocks a BUY/SELL while
        price is inside the ±VWAP_DEAD_ZONE_SIGMA band around VWAP, forcing HOLD
        in the no-trade zone. Only applies when VWAP fields are valid (respects
        the existing MIN_VWAP_BARS gating that exposes them as None early in the
        session).
        """
        if action not in ("BUY", "SELL"):
            return None
        if not indicators:
            return None
        vwap = indicators.get("vwap")
        if vwap is None or current_price <= 0:
            return None  # VWAP gated (too few intraday bars) -> no dead zone
        sigma = float(getattr(config, "VWAP_DEAD_ZONE_SIGMA", 1.0))
        # Use the precomputed ±1σ band when available; else derive from vwap.
        upper = indicators.get("vwap_upper_1")
        lower = indicators.get("vwap_lower_1")
        if upper is None or lower is None:
            return None
        if lower <= current_price <= upper:
            return (f"Rejected: VWAP dead zone. {symbol} at ${current_price:.2f} is inside "
                    f"the ±{sigma:.0f}σ band (${lower:.2f}–${upper:.2f}) around VWAP "
                    f"${vwap:.2f}. HOLD in the no-trade zone to avoid whipsawing "
                    f"around VWAP.")
        return None

    def _round_trip_budget_reason(self, symbol: str, action: str) -> str | None:
        """Return a rejection reason if the symbol has hit its daily round-trip budget.

        Fix 3: cap churn regardless of what the LLM says. Counts today's CLOSED
        round-trips (from core.feedback FIFO). Once the count >=
        MAX_ROUND_TRIPS_PER_DAY, block new BUYs (a new BUY would open yet another
        round-trip). A SELL is still allowed (it closes, not opens, a round-trip).
        """
        if action != "BUY":
            return None
        max_rt = int(getattr(config, "MAX_ROUND_TRIPS_PER_DAY", 2))
        if max_rt <= 0:
            return None
        try:
            from core.feedback import compute_closed_round_trips
            today = datetime.utcnow().strftime("%Y-%m-%d")
            trips = compute_closed_round_trips()
            sym = normalize_symbol(symbol)
            closed_today = sum(
                1 for t in trips
                if normalize_symbol(t["symbol"]) == sym
                and str(t.get("close_ts", "")).startswith(today)
            )
            if closed_today >= max_rt:
                return (f"Rejected: Daily round-trip budget. {symbol} has {closed_today} "
                        f"closed round-trip(s) today >= MAX_ROUND_TRIPS_PER_DAY {max_rt}. "
                        f"HOLD to stop churn.")
        except Exception as err:
            logger.error(f"Error checking round-trip budget for {symbol}: {err}")
        return None

    def _min_edge_reason(self, symbol: str, action: str, current_price: float) -> str | None:
        """Return a rejection reason if a REVERSAL lacks sufficient edge.

        Fix 5: a reversal (BUY after a recent SELL, or SELL after a recent BUY on
        the same symbol within the day) requires the price move from the last fill
        to exceed MIN_EDGE_PCT. Otherwise reject as "insufficient edge to justify
        reversal." Kills micro-whipsaw in tight ranges where the gross move is
        smaller than spread+fees.
        """
        if action not in ("BUY", "SELL"):
            return None
        min_edge = float(getattr(config, "MIN_EDGE_PCT", 0.3))
        if min_edge <= 0 or current_price <= 0:
            return None
        try:
            from core import database
            recent = database.get_recent_trades(limit=50)
            sym = normalize_symbol(symbol)
            today = datetime.utcnow().strftime("%Y-%m-%d")
            last_fill = None
            for t in recent:
                if (normalize_symbol(t.get("symbol", "")) == sym
                        and str(t.get("timestamp", "")).startswith(today)
                        and str(t.get("status", "")) in ("filled", "partially_filled")
                        and t.get("filled_avg_price")):
                    last_fill = t
                    break
            if not last_fill:
                return None
            last_side = str(last_fill.get("side", "")).lower()
            last_price = float(last_fill.get("filled_avg_price", 0.0))
            if last_price <= 0:
                return None
            # A reversal is BUY after SELL, or SELL after BUY.
            is_reversal = (action == "BUY" and last_side == "sell") or \
                          (action == "SELL" and last_side == "buy")
            if not is_reversal:
                return None
            move_pct = abs(current_price - last_price) / last_price * 100.0
            if move_pct < min_edge:
                return (f"Rejected: Insufficient edge for reversal. {symbol} last "
                        f"{last_side.upper()} at ${last_price:.2f}, now ${current_price:.2f} "
                        f"({move_pct:.2f}% move < MIN_EDGE_PCT {min_edge:.2f}%). "
                        f"Reversing would churn without covering spread+fees.")
        except Exception as err:
            logger.error(f"Error checking min-edge for {symbol}: {err}")
        return None

    def _day_direction_lock_reason(self, symbol: str, action: str, current_price: float,
                                   indicators: dict | None) -> str | None:
        """Return a rejection reason if a reversal violates the day-direction lock.

        Fix 6: once the brain takes a direction on a ticker intraday, a reversal
        is blocked unless price crosses a VWAP band by more than
        DAY_DIRECTION_LOCK_SIGMA OR moves more than DAY_DIRECTION_LOCK_MOVE_PCT
        from the last fill — a real regime change, not noise.
        """
        if action not in ("BUY", "SELL"):
            return None
        try:
            from core import database
            recent = database.get_recent_trades(limit=50)
            sym = normalize_symbol(symbol)
            today = datetime.utcnow().strftime("%Y-%m-%d")
            last_fill = None
            for t in recent:
                if (normalize_symbol(t.get("symbol", "")) == sym
                        and str(t.get("timestamp", "")).startswith(today)
                        and str(t.get("status", "")) in ("filled", "partially_filled")
                        and t.get("filled_avg_price")):
                    last_fill = t
                    break
            if not last_fill:
                return None
            last_side = str(last_fill.get("side", "")).lower()
            last_price = float(last_fill.get("filled_avg_price", 0.0))
            if last_price <= 0:
                return None
            is_reversal = (action == "BUY" and last_side == "sell") or \
                          (action == "SELL" and last_side == "buy")
            if not is_reversal:
                return None
            # Regime-change test: VWAP band cross OR large price move.
            move_pct = abs(current_price - last_price) / last_price * 100.0
            move_threshold = float(getattr(config, "DAY_DIRECTION_LOCK_MOVE_PCT", 0.5))
            if move_pct >= move_threshold:
                return None  # big move = real regime change, allow reversal
            if indicators:
                vwap = indicators.get("vwap")
                upper = indicators.get("vwap_upper_1")
                lower = indicators.get("vwap_lower_1")
                sigma = float(getattr(config, "DAY_DIRECTION_LOCK_SIGMA", 1.0))
                if vwap is not None and upper is not None and lower is not None:
                    # Did price cross a VWAP band boundary vs the last fill?
                    crossed = (last_price <= upper < current_price) or \
                              (last_price >= lower > current_price) or \
                              (last_price <= lower < current_price) or \
                              (last_price >= upper > current_price)
                    if crossed:
                        return None  # crossed a VWAP band = regime change
            return (f"Rejected: Day-direction lock. {symbol} last {last_side.upper()} at "
                    f"${last_price:.2f}, now ${current_price:.2f} ({move_pct:.2f}% move). "
                    f"Reversal blocked without a regime change (VWAP band cross or "
                    f"> {move_threshold:.2f}% move).")
        except Exception as err:
            logger.error(f"Error checking day-direction lock for {symbol}: {err}")
        return None

    def validate_and_adjust_decision(self, decision: dict, account_state: dict, current_positions: dict,
                                     cycle_context: dict | None = None) -> tuple[bool, str, dict]:
        """
        Validates the proposed decision from the LLM trading brain against risk limits.
        If necessary, it safely scales down trade size (shares) to comply with guidelines.
        
        Args:
            cycle_context (optional): mutable dict tracking cumulative spend / trade
                count across ALL decisions in the current cycle. Passed by the runner
                so multiple per-ticker BUYs share ONE budget. Expected keys:
                {"spent": float, "trades_executed": int, "equity": float}.
        
        Returns:
            - is_approved (bool)
            - status_message (str)
            - adjusted_decision (dict): Copy of decision with adjusted quantities if needed.
        """
        action = decision.get("action", "HOLD").upper()
        symbol = decision.get("symbol", "").upper()
        proposed_qty = float(decision.get("quantity", 0.0))
        
        adjusted_decision = decision.copy()
        adjusted_decision["action"] = action
        adjusted_decision["symbol"] = symbol
        adjusted_decision["quantity"] = proposed_qty

        # Cycle-level limits: enforce MAX_TRADES_PER_CYCLE + cumulative budget.
        # cycle_context is shared across all decisions in this cycle.
        cycle_context = cycle_context if isinstance(cycle_context, dict) else {}

        # 1. HOLD/NO_ACTION requires no guardrail checks
        if action in ("HOLD", "NO_ACTION"):
            adjusted_decision["quantity"] = 0.0
            return True, "Approved: No action taken.", adjusted_decision

        # 1b. Per-cycle trade cap
        max_trades = int(getattr(config, "MAX_TRADES_PER_CYCLE", 1))
        trades_so_far = int(cycle_context.get("trades", 0))
        # A SELL that reduces an existing held position is an EXIT (risk
        # reduction), not new risk-taking. Exempt it from the per-cycle cap so a
        # single executed trade earlier in the cycle can never block a needed
        # de-risk/exit. BUYs (and SELLs that open a position we don't hold, e.g.
        # a short) still count against the cap.
        held_qty = 0.0
        if action == "SELL":
            held = current_positions.get(symbol) or current_positions.get(symbol.replace("/", ""))
            if isinstance(held, dict):
                held_qty = float(held.get("qty", 0.0) or 0.0)
        is_exit_sell = action == "SELL" and held_qty > 0 and proposed_qty > 0
        if (action in ("BUY", "SELL") and proposed_qty > 0 and not is_exit_sell
                and trades_so_far >= max_trades):
            adjusted_decision["quantity"] = 0.0
            return False, (f"Rejected: Per-cycle trade cap reached ({trades_so_far} >= "
                           f"MAX_TRADES_PER_CYCLE {max_trades}). No more executable trades this cycle."), adjusted_decision

        # 2. Check Action validity
        if action not in ("BUY", "SELL"):
            return False, f"Rejected: Unknown action '{action}'. Only BUY, SELL, or HOLD are permitted.", adjusted_decision

        # 2b. Resolve instrument (stock vs option).
        # The agent now outputs an EXPLICIT instrument intent ("stock"/"option")
        # alongside its directional view + conviction. This guardrail RESPECTS
        # the model's stated intent, using conviction as a GATE (not a forced
        # route): options are only used when the model explicitly said "option"
        # AND conviction >= threshold. This prevents stock<->option whipsaw AND
        # prevents a "moderate, I'll buy shares" decision from being silently
        # converted into an option contract.
        # NOTE: This guardrail only routes NEW option BUYs. Existing option
        # positions (SELL) are detected separately below by their OCC symbol.
        instrument = None
        if is_occ_symbol(symbol):
            instrument = "option"  # closing an existing option position
        elif action == "BUY":
            conviction = float(decision.get("conviction", 0.0) or 0.0)
            options_on = getattr(config, "OPTIONS_ENABLED", False)
            in_universe = symbol.upper() in set(getattr(config, "OPTIONS_UNIVERSE", []))
            threshold = float(getattr(config, "OPTIONS_CONVICTION_THRESHOLD", 0.7))
            # Model's explicit instrument intent (empty/none -> default stock).
            requested = str(decision.get("instrument", "") or "").lower().strip()
            # Direction must support a leveraged long (bullish -> call, bearish -> put).
            direction = str(decision.get("direction", "neutral")).lower()
            opts_requested = (requested == "option")
            opts_eligible = (options_on and in_universe
                             and conviction >= threshold
                             and direction in ("bullish", "bearish"))
            if opts_requested and not opts_eligible:
                # The brain explicitly asked for an option but the symbol is not
                # options-eligible (not in OPTIONS_UNIVERSE, options disabled,
                # conviction below threshold, or neutral direction). Reject the
                # BUY outright instead of silently downgrading to a stock buy.
                # This prevents the "wanted a put, got stock" mismatch (AMGN
                # 2026-09-10) where a bearish option intent became an unintended
                # long stock position that the brain then tried to unwind.
                reason = self._option_intent_rejection_reason(
                    symbol, options_on, in_universe, conviction, threshold, direction
                )
                adjusted_decision["quantity"] = 0.0
                return False, reason, adjusted_decision
            if opts_eligible and opts_requested:
                instrument = "option"
                adjusted_decision["instrument"] = "option"
            else:
                instrument = "stock"
                adjusted_decision["instrument"] = "stock"
        else:
            adjusted_decision["instrument"] = "stock"

        # 2c. Options-specific validation (BUY-to-open of a new option position,
        # or SELL-to-close of an existing OCC option position).
        if instrument == "option":
            ok, msg, adjusted = self._validate_option_decision(
                decision, adjusted_decision, account_state, current_positions
            )
            if not ok:
                return False, msg, adjusted
            return True, msg, adjusted

        # 3. Check Universe Restrictions
        # Allow trading if symbol is in config.TRADING_UNIVERSE, in the latest
        # screened watchlist, OR is a currently-held position. Held positions must
        # always be tradable so the agent can manage (SELL) or add to (BUY) them,
        # even if the symbol is not in the static universe or the latest top-N
        # watchlist (e.g. a position opened earlier that later fell out of the
        # top-N screener output).
        from core import database
        allowed_symbols = set(s.upper() for s in config.TRADING_UNIVERSE)
        
        latest_watchlist = database.get_latest_watchlist_raw()
        for ws_symbol in latest_watchlist:
            # Normalize watchlist symbols (e.g. SOL/USD -> SOLUSD) and add both forms
            allowed_symbols.add(ws_symbol.upper())
            allowed_symbols.add(ws_symbol.upper().replace('/', ''))
            
        # Add currently-held positions (both raw and normalized forms)
        for pos_symbol in current_positions:
            allowed_symbols.add(pos_symbol.upper())
            allowed_symbols.add(pos_symbol.upper().replace('/', ''))
            
        normalized_symbol = symbol.upper().replace('/', '')
        if symbol.upper() not in allowed_symbols and normalized_symbol not in allowed_symbols:
            return False, f"Rejected: Symbol '{symbol}' is not in the allowed trading universe ({config.TRADING_UNIVERSE}) nor in the latest watchlist ({latest_watchlist}).", adjusted_decision

        # 3b. Check Market Hours for Equities (Crypto is 24/7)
        is_crypto = "SOL" in symbol or "USD" in symbol or "/" in symbol
        if not is_crypto:
            is_open, msg = self.is_market_open_check()
            if not is_open:
                return False, f"Rejected: {msg} Equities trading restricted outside regular market hours.", adjusted_decision

        # 3c. Check Anti-Whipsaw Holding Period (4 hours)
        try:
            from core import database
            recent_trades = database.get_recent_trades(limit=10)
            last_trade = None
            # Treat any EXECUTED status as a recent trade so a partial fill can't
            # disarm the whipsaw guard. Only failed/canceled orders are ignored
            # (they never actually moved shares). Fix 2 (2026-09-09): previously
            # only matched status == "filled", so a "partially_filled" buy (e.g.
            # WFC 16:18) fell through to an older trade and let a same-day SELL
            # (16:43) slip through the 4-hour holding guard.
            _executed_statuses = ("filled", "partially_filled", "submitted", "open")
            for t in recent_trades:
                if t["symbol"] == symbol and t["status"] in _executed_statuses:
                    last_trade = t
                    break
                    
            if last_trade:
                trade_time_str = last_trade["timestamp"]
                if trade_time_str.endswith("Z"):
                    trade_time_str = trade_time_str[:-1]
                trade_time = datetime.fromisoformat(trade_time_str)
                # Normalize to a NAIVE UTC datetime. Broker-reconcile/ahead-of-time
                # timestamps can carry a '+00:00' (or 'Z') offset; datetime.utcnow()
                # is naive, so subtracting an AWARE datetime would raise
                # "can't subtract offset-naive and offset-aware datetimes".
                if trade_time.tzinfo is not None:
                    trade_time = trade_time.astimezone(timezone.utc).replace(tzinfo=None)
                time_diff = datetime.utcnow() - trade_time
                hours_since_last_trade = time_diff.total_seconds() / 3600.0
                
                MIN_HOLDING_HOURS = 4.0
                if hours_since_last_trade < MIN_HOLDING_HOURS:
                    last_side = last_trade["side"].upper()
                    if last_side == "BUY" and action == "SELL":
                        # A SELL that exits a LOSING position (current price below
                        # the position's average entry) is a risk-reduction / stop-
                        # loss exit, NOT churn. Blocking it traps the book in a
                        # falling position (e.g. DOT/USD scaled in all day, price
                        # dropped below entry, brain wanted to exit but was blocked
                        # every cycle). Only block a SELL that would churn a
                        # profitable/flat position within the holding window.
                        held = current_positions.get(symbol) or current_positions.get(symbol.replace("/", ""))
                        avg_entry = 0.0
                        if isinstance(held, dict):
                            avg_entry = float(held.get("avg_entry_price", 0.0) or 0.0)
                        current_price = float(decision.get("current_price", 0.0) or 0.0)
                        is_losing_exit = (avg_entry > 0 and current_price > 0
                                          and current_price < avg_entry)
                        if not is_losing_exit:
                            return False, f"Rejected: Anti-whipsaw guardrail. Asset bought too recently ({hours_since_last_trade:.2f} hours ago < {MIN_HOLDING_HOURS} hours limit). Selling blocked.", adjusted_decision
                    elif last_side == "SELL" and action == "BUY":
                        return False, f"Rejected: Anti-whipsaw guardrail. Asset sold too recently ({hours_since_last_trade:.2f} hours ago < {MIN_HOLDING_HOURS} hours limit). Re-buying blocked.", adjusted_decision
        except Exception as err:
            logger.error(f"Error checking anti-whipsaw guardrail: {err}")

        # 3d. Whipsaw-prevention guardrails (2026-09-09). These target the
        # WFC-style intraday buy/sell flip-flop in a tight range around VWAP.
        # They apply to both BUY and SELL (a reversal in either direction churns).
        indicators = decision.get("indicators") if isinstance(decision.get("indicators"), dict) else {}
        current_price = float(decision.get("current_price", 0.0))
        if current_price <= 0:
            return False, "Rejected: Missing or invalid current price for execution.", adjusted_decision

        # 3d-1. VWAP dead zone (Fix 1): block BUY/SELL inside the ±1σ band.
        vwap_reason = self._vwap_dead_zone_reason(symbol, action, current_price, indicators)
        if vwap_reason:
            adjusted_decision["quantity"] = 0.0
            return False, vwap_reason, adjusted_decision

        # 3d-2. Minimum-edge gate for reversals (Fix 5).
        edge_reason = self._min_edge_reason(symbol, action, current_price)
        if edge_reason:
            adjusted_decision["quantity"] = 0.0
            return False, edge_reason, adjusted_decision

        # 3d-3. Day-direction lock (Fix 6).
        lock_reason = self._day_direction_lock_reason(symbol, action, current_price, indicators)
        if lock_reason:
            adjusted_decision["quantity"] = 0.0
            return False, lock_reason, adjusted_decision

        # 4. Check Daily Loss Limit (Equity Drawdown Guardrail)
        equity = account_state.get("equity", 0.0)
        cash = account_state.get("cash", 0.0)
        unrealized_pnl = account_state.get("unrealized_pnl", 0.0)
        
        # Alpaca provides 'last_equity' which is equity at previous market close
        # If unavailable (e.g. mock), we assume starting equity was close to current
        last_equity = account_state.get("last_equity", equity)
        if last_equity > 0:
            daily_drawdown_pct = (last_equity - equity) / last_equity
            if daily_drawdown_pct >= config.DAILY_LOSS_LIMIT_PCT:
                # If we've hit the loss limit, only allow selling (to liquidate/de-risk)
                if action == "BUY":
                    return False, f"Rejected: Daily loss limit exceeded ({daily_drawdown_pct:.2%} >= {config.DAILY_LOSS_LIMIT_PCT:.2%}). All BUY orders blocked.", adjusted_decision

        # Fetch current price (already resolved above for the whipsaw guardrails)
        if current_price <= 0:
            return False, "Rejected: Missing or invalid current price for execution.", adjusted_decision

        # 5. Sell Rules Guardrail
        if action == "SELL":
            # Check if we own this asset
            if symbol not in current_positions:
                return False, f"Rejected: Attempted to sell {symbol} but do not own any shares.", adjusted_decision
            
            owned_qty = current_positions[symbol]["qty"]
            qty_available = current_positions[symbol].get("qty_available", owned_qty)

            # Locked-shares case: ALL shares are reserved by open bracket (TP/SL)
            # orders (qty_available == 0) but we still own the position. The
            # executor's SELL path cancels open orders and waits for shares to
            # release before selling, so approve a FULL liquidation here and let
            # the executor cancel-and-sell. This unblocks positions that would
            # otherwise be stuck "deferred" forever because nothing ever cancels
            # the bracket legs. Checked BEFORE the scaling block so a positive
            # proposed_qty isn't zeroed out first.
            if qty_available == 0:
                if owned_qty <= 0:
                    adjusted_decision["quantity"] = 0.0
                    return False, f"Rejected: Attempted to sell {symbol} but do not own any shares.", adjusted_decision
                logger.warning(
                    f"All {owned_qty} shares of {symbol} are locked in open orders "
                    f"(qty_available=0). Approving full-liquidation SELL so the "
                    f"executor can cancel the bracket legs and release the shares."
                )
                adjusted_decision["quantity"] = owned_qty
                proposed_qty = owned_qty
                return True, f"Approved: Full-liquidation SELL of {owned_qty} shares of {symbol} (all shares locked in open orders; executor will cancel and release).", adjusted_decision

            # If the proposed quantity exceeds qty_available, log a warning and scale it down to qty_available.
            if proposed_qty > qty_available:
                logger.warning(f"Adjusting SELL quantity for {symbol} from {proposed_qty} to available {qty_available} (owned: {owned_qty}, locked in other orders: {owned_qty - qty_available}).")
                adjusted_decision["quantity"] = qty_available
                proposed_qty = qty_available

            if proposed_qty <= 0:
                adjusted_decision["quantity"] = 0.0
                return False, f"Deferred: Sell of {symbol} deferred because proposed quantity is zero.", adjusted_decision

            # Dust-liquidation guardrail: if the whole position (or the proposed
            # sell) is worth less than MIN_SELL_VALUE, escalate to a full exit so
            # the agent never bleeds out a tiny fraction (e.g. 25% of a small
            # position) every cycle. This is deterministic and protects against
            # any LLM-generated rule emitting fractional sells.
            position_value = owned_qty * current_price
            escalated = False
            if position_value <= config.MIN_SELL_VALUE:
                logger.warning(f"Dust position for {symbol}: value ${position_value:.2f} <= MIN_SELL_VALUE ${config.MIN_SELL_VALUE:.2f}. Escalating SELL to full liquidation of {qty_available} shares.")
                adjusted_decision["quantity"] = qty_available
                proposed_qty = qty_available
                escalated = True
            elif proposed_qty * current_price < config.MIN_SELL_VALUE:
                logger.warning(f"Proposed SELL value for {symbol} (${proposed_qty * current_price:.2f}) is below MIN_SELL_VALUE ${config.MIN_SELL_VALUE:.2f}. Escalating to full liquidation of {qty_available} shares.")
                adjusted_decision["quantity"] = qty_available
                proposed_qty = qty_available
                escalated = True

            if escalated:
                return True, f"Approved: Dust-liquidation full exit of {proposed_qty} shares of {symbol} (position value ${position_value:.2f} <= MIN_SELL_VALUE ${config.MIN_SELL_VALUE:.2f}).", adjusted_decision
                
            return True, f"Approved: Sell order of {proposed_qty} shares of {symbol} validated.", adjusted_decision

        # 6. Buy Rules Guardrail
        if action == "BUY":
            # 6a. Strict-universe guardrail: a NEW buy must be screener-endorsed
            # (in the latest watchlist) OR crypto OR already held. Untracked names
            # in the static TRADING_UNIVERSE that the screener never picks are
            # blocked from being newly bought, closing the fallback-universe loss hole.
            strict_reason = self._universe_guardrail_reason(symbol, current_positions)
            if strict_reason:
                adjusted_decision["quantity"] = 0.0
                return False, strict_reason, adjusted_decision

            # 6a2. Anti-scale-in guardrail: block averaging DOWN into a held
            # position that is NOT screener-endorsed. Prevents the MS
            # "buy-the-dip-adding" failure mode that bled -$226. Screener-endorsed
            # (currently-watchlisted) names are exempt so a high-conviction add to
            # a sanctioned symbol isn't wrongly blocked.
            scale_in_reason = self._anti_scale_in_reason(
                symbol, proposed_qty, current_price, current_positions
            )
            if scale_in_reason:
                adjusted_decision["quantity"] = 0.0
                return False, scale_in_reason, adjusted_decision

            # Per-ticker loss / whipsaw circuit breaker: block re-entering a
            # symbol that has repeatedly lost money or whipsaws, so the strategy
            # stops bleeding on the same names (e.g. INTC/AMD/SPY in the data).
            breaker_reason = self._circuit_breaker_reason(symbol)
            if breaker_reason:
                adjusted_decision["quantity"] = 0.0
                return False, breaker_reason, adjusted_decision

            # Daily round-trip budget (Fix 3): cap churn even on a symbol that
            # passes the circuit breakers. Runs AFTER the breakers so the more
            # specific loss/whipsaw breakers take precedence.
            rt_reason = self._round_trip_budget_reason(symbol, action)
            if rt_reason:
                adjusted_decision["quantity"] = 0.0
                return False, rt_reason, adjusted_decision

            # Intra-day PnL circuit breaker: block new BUYs when the day's
            # realized + unrealized loss exceeds the limit (SELLs still allowed).
            intraday_reason = self._intraday_pnl_breaker_reason(account_state)
            if intraday_reason:
                adjusted_decision["quantity"] = 0.0
                return False, intraday_reason, adjusted_decision

            # Max dollar allocation allowed for a single trade
            max_trade_value = equity * config.MAX_TRADE_ALLOCATION_PCT
            proposed_trade_value = proposed_qty * current_price
            
            # Check if transaction value exceeds max allocation limit
            if proposed_trade_value > max_trade_value:
                # Fractional quantities are preserved for both crypto and equities.
                # The broker layer (alpaca_client) handles eligibility: bracket (OCO)
                # orders round to whole shares, and plain market orders fall back to
                # whole shares if Alpaca rejects a fractional qty for a given equity.
                max_allowed_qty = round(max_trade_value / current_price, 4)
                    
                logger.warning(f"Proposed buy value (${proposed_trade_value:,.2f}) exceeds max trade size limit (${max_trade_value:,.2f}). "
                               f"Scaling down quantity from {proposed_qty} to {max_allowed_qty}.")
                proposed_qty = max_allowed_qty
                proposed_trade_value = proposed_qty * current_price

            # Volatility-based position sizing: scale down the max allocation for
            # high-volatility assets so the same dollar *risk* is taken regardless
            # of asset. A symbol with ATR% above the baseline gets its max trade
            # value scaled by (baseline / atr_pct), floored at a min allocation.
            if getattr(config, "VOL_SIZING_ENABLED", True):
                atr_pct = decision.get("atr_pct")
                if atr_pct and atr_pct > 0:
                    baseline = float(getattr(config, "VOL_SIZING_BASELINE_ATR_PCT", 2.0))
                    min_alloc = float(getattr(config, "VOL_SIZING_MIN_ALLOCATION_PCT", 0.02))
                    if atr_pct > baseline:
                        scale = baseline / atr_pct
                        vol_capped_value = max_trade_value * scale
                        min_value = equity * min_alloc
                        vol_capped_value = max(vol_capped_value, min_value)
                        if proposed_trade_value > vol_capped_value:
                            vol_qty = round(vol_capped_value / current_price, 4)
                            logger.warning(
                                f"Volatility-based sizing for {symbol}: ATR%={atr_pct:.2f}% > baseline {baseline:.2f}%. "
                                f"Capping buy value from ${proposed_trade_value:,.2f} to ${vol_capped_value:,.2f} "
                                f"(scale {scale:.2f}x)."
                            )
                            proposed_qty = vol_qty
                            proposed_trade_value = proposed_qty * current_price

            # Cumulative cycle budget: cap total spend across ALL buys this cycle so
            # N per-ticker BUYs can't each pass the per-trade check while summing far
            # beyond acceptable exposure.
            spent_so_far = float(cycle_context.get("spent", 0.0))
            # Total allowed cycle spend = per-trade cap * max trades (bounded by none).
            max_cycle_budget = max_trade_value * int(getattr(config, "MAX_TRADES_PER_CYCLE", 1))
            if spent_so_far + proposed_trade_value > max_cycle_budget:
                allowed_for_cycle = max(0.0, max_cycle_budget - spent_so_far)
                if allowed_for_cycle <= 0:
                    adjusted_decision["quantity"] = 0.0
                    return False, (f"Rejected: Cumulative cycle spend budget exhausted "
                                   f"(${spent_so_far:,.2f} spent / ${max_cycle_budget:,.2f})."), adjusted_decision
                max_allowed_qty = round(allowed_for_cycle / current_price, 4)
                logger.warning(f"Cycle spend budget would be exceeded (${spent_so_far+proposed_trade_value:,.2f} > ${max_cycle_budget:,.2f}). "
                               f"Scaling buy for {symbol} to ${allowed_for_cycle:,.2f}.")
                proposed_qty = max(0.0, max_allowed_qty)
                proposed_trade_value = proposed_qty * current_price
                if proposed_qty <= 0:
                    adjusted_decision["quantity"] = 0.0
                    return False, f"Rejected: Cumulative cycle budget exhausted for {symbol}.", adjusted_decision
                
                
            # Check per-ticker allocation limit
            existing_position_value = 0.0
            if symbol in current_positions:
                existing_position_value = current_positions[symbol]["qty"] * current_price
                
            max_ticker_value = equity * config.MAX_TICKER_ALLOCATION_PCT
            total_proposed_value = existing_position_value + proposed_trade_value
            
            if total_proposed_value > max_ticker_value:
                max_allowed_value = max(0, max_ticker_value - existing_position_value)
                # Fractional quantities preserved for both crypto and equities (see note above).
                max_allowed_qty = round(max_allowed_value / current_price, 4)
                    
                logger.warning(f"Proposed total position value (${total_proposed_value:,.2f}) exceeds per-ticker limit (${max_ticker_value:,.2f}). "
                               f"Scaling down quantity from {proposed_qty} to {max_allowed_qty}.")
                proposed_qty = max_allowed_qty
                proposed_trade_value = proposed_qty * current_price
                
                if proposed_qty <= 0:
                    adjusted_decision["quantity"] = 0.0
                    return False, f"Rejected: Buy quantity scaled down to 0 because total position allocation for {symbol} would exceed the per-ticker limit of {config.MAX_TICKER_ALLOCATION_PCT * 100}% of equity.", adjusted_decision

            # Check Correlation / Concentration cluster limit.
            # Caps TOTAL exposure across all correlated symbols sharing a cluster
            # (e.g. BTC+ETH+SOL crypto book, or tech mega-caps) to keep the
            # portfolio diversified and avoid an oversized single-theme bet.
            cluster, cluster_exposure = self._cluster_exposure(
                symbol, proposed_trade_value, current_positions
            )
            max_cluster_value = equity * getattr(config, "MAX_CLUSTER_ALLOCATION_PCT", 0.40)
            if cluster_exposure > max_cluster_value:
                allowed_for_cluster = max(0.0, max_cluster_value -
                                          (cluster_exposure - proposed_trade_value))
                new_allowed_qty = round(allowed_for_cluster / current_price, 4) if current_price > 0 else 0.0
                if new_allowed_qty <= 0:
                    adjusted_decision["quantity"] = 0.0
                    return False, (f"Rejected: Cluster '{cluster}' concentration limit would be "
                                   f"exceeded (${cluster_exposure:,.2f} > ${max_cluster_value:,.2f}). "
                                   f"No room for new {symbol} buys in this correlated cluster."), adjusted_decision
                logger.warning(f"Cluster '{cluster}' exposure (${cluster_exposure:,.2f}) exceeds "
                               f"MAX_CLUSTER_ALLOCATION_PCT (${max_cluster_value:,.2f}). "
                               f"Scaling {symbol} buy from {proposed_qty} to {new_allowed_qty}.")
                proposed_qty = new_allowed_qty
                proposed_trade_value = proposed_qty * current_price
                if proposed_qty <= 0:
                    adjusted_decision["quantity"] = 0.0
                    return False, f"Rejected: Cluster '{cluster}' concentration limit exhausted for {symbol}.", adjusted_decision

            # Ensure we maintain the cash buffer
            # Calculate dynamic cash buffer based on number of open positions
            num_open_positions = len(current_positions)
            
            # Base buffer percentage (e.g., 5%)
            base_buffer_pct = 0.05
            
            # Additional buffer per open position (e.g., 1%)
            per_position_buffer_pct = 0.01
            
            # Maximum buffer percentage (e.g., 25%)
            max_buffer_pct = 0.25
            
            # Calculate dynamic buffer percentage
            dynamic_buffer_pct = min(max_buffer_pct, base_buffer_pct + (num_open_positions * per_position_buffer_pct))
            
            min_cash_required = equity * dynamic_buffer_pct
            
            available_spending_cash = cash - min_cash_required
            
            if available_spending_cash <= 0:
                return False, f"Rejected: Cash balance (${cash:,.2f}) is below or near the required cash buffer (${min_cash_required:,.2f}). Buying blocked.", adjusted_decision
                
            if proposed_trade_value > available_spending_cash:
                # Fractional quantities preserved for both crypto and equities (see note above).
                max_cash_qty = round(available_spending_cash / current_price, 4)
                    
                logger.warning(f"Proposed buy cost (${proposed_trade_value:,.2f}) exceeds available spending cash (${available_spending_cash:,.2f}). "
                               f"Scaling down quantity from {proposed_qty} to {max_cash_qty}.")
                proposed_qty = max_cash_qty
                proposed_trade_value = proposed_qty * current_price

            if proposed_qty <= 0:
                return False, "Rejected: Buy quantity scaled down to 0 due to cash limits.", adjusted_decision

            # Whole-share floor for equities (Fix F, 2026-09-10): Alpaca
            # rejects fractional-share bracket (OCO) orders for equities with
            # "cost basis must be >= minimal amount of order 1" (seen on AMGN
            # 2026-09-10 when the brain proposed qty 0.00012). Floor equity
            # buys to whole shares so a valid, high-conviction BUY never dies on a
            # fractional-share artifact. Crypto keeps fractional quantities (it is
            # the profitable book and Alpaca supports fractional crypto).
            if not is_crypto_member(symbol):
                floored_qty = math.floor(proposed_qty)
                if floored_qty < 1:
                    adjusted_decision["quantity"] = 0.0
                    return False, (f"Rejected: Buy quantity for {symbol} floored to "
                                    f"{floored_qty} whole share(s) (proposed {proposed_qty:.4f}). "
                                    f"Position too small to buy a whole share."), adjusted_decision
                if floored_qty != proposed_qty:
                    logger.warning(
                        f"Whole-share floor for {symbol}: rounding buy qty from "
                        f"{proposed_qty:.4f} to {floored_qty} shares (Alpaca rejects "
                        f"fractional equity bracket orders)."
                    )
                    proposed_qty = floored_qty
                    proposed_trade_value = proposed_qty * current_price
            else:
                # Minimum-notional floor for crypto (Fix G, 2026-09-11): Alpaca
                # rejects crypto orders below its per-pair minimum with
                # "cost basis must be >= minimal amount of order 10" (seen on
                # DOT/USD 2026-09-11). Crypto keeps fractional quantities, so the
                # equity whole-share floor above doesn't apply. Reject (never
                # submit) a crypto BUY whose dollar notional is below the broker
                # minimum so the rejection can't bubble up as a Jira-filing ERROR
                # in the client and a CRITICAL in the runner.
                min_notional = float(getattr(config, "MIN_CRYPTO_ORDER_NOTIONAL", 10.0))
                if proposed_trade_value < min_notional:
                    adjusted_decision["quantity"] = 0.0
                    return False, (f"Rejected: Crypto BUY notional for {symbol} "
                                    f"(${proposed_trade_value:.2f}) is below Alpaca's "
                                    f"minimum order size (${min_notional:.2f})."), adjusted_decision

            adjusted_decision["quantity"] = proposed_qty
            return True, f"Approved: Buy order of {proposed_qty} shares of {symbol} validated.", adjusted_decision

        return False, "Rejected: Fell through all guardrail options.", adjusted_decision

    # ------------------------------------------------------------------
    # OPTIONS VALIDATION
    # ------------------------------------------------------------------

    def _option_intent_rejection_reason(self, symbol: str, options_on: bool,
                                        in_universe: bool, conviction: float,
                                        threshold: float, direction: str) -> str:
        """Build a human-readable rejection reason for an ineligible option intent.

        Called when the brain explicitly requested ``instrument: "option"`` but the
        symbol is not options-eligible. Returns a specific reason so the decision
        stream shows exactly why the option BUY was rejected (rather than silently
        converting it to a stock buy).
        """
        if not options_on:
            return (f"Rejected: Option intent for {symbol} but options trading is "
                    f"disabled (OPTIONS_ENABLED=false). No trade taken.")
        if not in_universe:
            return (f"Rejected: Option intent for {symbol} but {symbol} is not in the "
                    f"options universe. No trade taken (refusing to silently buy stock "
                    f"for an option intent).")
        if direction not in ("bullish", "bearish"):
            return (f"Rejected: Option intent for {symbol} but direction is "
                    f"'{direction}' (options require bullish/bearish). No trade taken.")
        return (f"Rejected: Option intent for {symbol} but conviction "
                f"{conviction:.2f} < threshold {threshold:.2f}. No trade taken.")

    def _validate_option_decision(self, decision: dict, adjusted_decision: dict,
                                  account_state: dict, current_positions: dict) -> tuple[bool, str, dict]:
        """Validates an option decision (BUY-to-open or SELL-to-close).

        Enforces: kill-switch, market hours, options universe, options buying
        power, per-ticker contract limits, DTE hard bounds, and the
        earnings/IV filter (no earnings inside the DTE window).
        """
        from core import database
        action = adjusted_decision.get("action", "HOLD").upper()
        symbol = adjusted_decision.get("symbol", "").upper()
        proposed_qty = float(adjusted_decision.get("quantity", 0.0))

        # Kill-switch
        if not getattr(config, "OPTIONS_ENABLED", False):
            return False, "Rejected: Options trading is disabled (OPTIONS_ENABLED=false).", adjusted_decision

        # Dedicated OPTIONS KILL SWITCH (independent of the global kill switch).
        # When HALTED, block opening NEW option positions (BUY-to-open). Existing
        # option positions may still be SELL-to-closed (de-risked) — the kill
        # switch stops new risk, never locks you into an existing position.
        action = adjusted_decision.get("action", "HOLD").upper()
        if action == "BUY":
            try:
                from core.gcs_sync import check_options_kill_switch
                opts_ks = check_options_kill_switch()
                status = str(opts_ks.get("status", "ACTIVE")).upper()
                if status == "HALTED":
                    return False, ("Rejected: Options KILL SWITCH is HALTED. "
                                   "New option positions are blocked. "
                                   "Stock/crypto trading is unaffected."), adjusted_decision
            except Exception as ks_err:
                logger.error(f"Error checking options kill switch: {ks_err}. Allowing option BUY.")
                # Fail-open on GCS errors so a transient GCS outage never
                # freezes legitimate option buying unexpectedly.

        cleared_o = symbol.replace(" ", "")
        # Determine underlying root + eligibility
        is_occ = is_occ_symbol(symbol)
        if is_occ:
            # SELL-to-close of an existing position: allow the underlying regardless
            underlying = re.match(r"^[A-Z]+", cleared_o).group(0).strip() or symbol
            # Verify we actually hold this contract
            held = current_positions.get(symbol) or current_positions.get(cleared_o)
            if not held or float(held.get("qty", 0)) <= 0:
                return False, f"Rejected: Attempted to SELL-to-close {symbol} but no position is held.", adjusted_decision
            # Cap sell qty at held qty (never go short)
            held_qty = float(held.get("qty", 0))
            if proposed_qty > held_qty:
                logger.warning(f"Option SELL qty {proposed_qty} capped to held {held_qty} for {symbol}.")
                adjusted_decision["quantity"] = held_qty
                proposed_qty = held_qty
        else:
            underlying = symbol.upper()
            # Market hours (options are equity-hours only)
            is_open, msg = self.is_market_open_check()
            if not is_open:
                return False, f"Rejected: {msg} Options trading restricted outside regular market hours.", adjusted_decision
            # Options universe membership
            if underlying not in set(getattr(config, "OPTIONS_UNIVERSE", [])):
                return False, f"Rejected: Symbol '{underlying}' is not in the options universe.", adjusted_decision
            # DTE hard bounds (agent override must stay within config bounds)
            dte_min = int(adjusted_decision.get("option_dte_min") or getattr(config, "OPTIONS_DTE_MIN", 30))
            dte_max = int(adjusted_decision.get("option_dte_max") or getattr(config, "OPTIONS_DTE_MAX", 45))
            hard_min = getattr(config, "OPTIONS_DTE_HARD_MIN", 14)
            hard_max = getattr(config, "OPTIONS_DTE_HARD_MAX", 90)
            if dte_min < hard_min:
                dte_min = hard_min
            if dte_max > hard_max:
                dte_max = hard_max
            if dte_min > dte_max:
                dte_min, dte_max = dte_max, dte_min
            adjusted_decision["option_dte_min"] = dte_min
            adjusted_decision["option_dte_max"] = dte_max
            # OTM% bounds
            otm = adjusted_decision.get("option_strike_otm_pct")
            if otm is not None:
                otm = max(0.0, min(0.30, float(otm)))
                adjusted_decision["option_strike_otm_pct"] = otm
            # Earnings/IV filter: reject if underlying reports earnings inside DTE window
            cleared_underlying = underlying
            earnings_msg = self._has_earnings_before_expiry(cleared_underlying, dte_max)
            if earnings_msg:
                return False, f"Rejected: {earnings_msg}", adjusted_decision

        # Buying power sizing (only for BUY-to-open; SELL just closes, no new capital)
        if action == "BUY":
            equity = float(account_state.get("equity", 0.0))
            max_cost = equity * float(getattr(config, "OPTIONS_MAX_ALLOCATION_PCT", 0.05))
            # Estimate premium per contract ~ 1-2% OTM; we use a placeholder until the
            # executor resolves the actual contract. The executor will enforce the real
            # cost. Here we cap proposed contracts by a config max and rough budget.
            max_contracts = getattr(config, "OPTIONS_MAX_CONTRACTS_PER_TICKER", 5)
            if proposed_qty > max_contracts:
                logger.warning(f"Option buy qty {proposed_qty} capped to max contracts {max_contracts} per ticker.")
                adjusted_decision["quantity"] = max_contracts
                proposed_qty = max_contracts
            # Options buying power check (upper bound)
            obp = float(self._get_options_buying_power())
            # Reserve at least ~equity*2% per contract as a sanity bound; the executor
            # does the exact ask*100*contracts check. Skip hard rejection here.
            if obp <= 0:
                return False, "Rejected: No options buying power available.", adjusted_decision

            # TOTAL OPTIONS EXPOSURE CAP: prevent the strategist's bearish-put
            # authorization from over-leveraging the book across many symbols.
            # Sum the market value of all currently-held option positions and
            # reject a new BUY-to-open if it would push total exposure above the
            # configured % of equity.
            try:
                from core.feedback import is_option_contract_symbol
                total_opt_mv = 0.0
                for pos_sym, pos in (current_positions or {}).items():
                    if is_option_contract_symbol(pos_sym):
                        total_opt_mv += float(pos.get("market_value", 0.0) or 0.0)
                max_total = equity * float(getattr(config, "OPTIONS_MAX_TOTAL_EXPOSURE_PCT", 0.15))
                if total_opt_mv >= max_total:
                    return False, (
                        f"Rejected: Total options exposure ${total_opt_mv:,.2f} already "
                        f"at/above the ${max_total:,.2f} cap "
                        f"({getattr(config, 'OPTIONS_MAX_TOTAL_EXPOSURE_PCT', 0.15)*100:.0f}% of equity). "
                        "New option BUY blocked."
                    ), adjusted_decision
            except Exception as exp_err:
                logger.warning(f"Total options exposure check failed (fail-open): {exp_err}")

        adjusted_decision["instrument"] = "option"
        return True, f"Approved: Option {'BUY-to-open' if action == 'BUY' else 'SELL-to-close'} via instrument rule.", adjusted_decision

    def get_options_buying_power(self) -> float:
        """Returns account options buying power (separate from equity buying power)."""
        return self._get_options_buying_power()

    def _get_options_buying_power(self) -> float:
        """Reads options buying power from the active Alpaca client (via a small cache)."""
        try:
            from core import alpaca_client as ac
            # get singleton client if it exists
            inst = getattr(ac, "_client_instance", None)
            if inst is None:
                return 0.0
            return inst.get_options_buying_power()
        except Exception as e:
            logger.error(f"Error fetching options buying power in guardrails: {e}")
            return 0.0

    def _has_earnings_before_expiry(self, underlying: str, dte_max: int) -> str:
        """Returns an error string if the underlying reports earnings within dte_max days; '' if clear."""
        try:
            from core import data_provider as dp
            earn = getattr(dp, "get_earnings_dates", None)
            if earn is None:
                return ""
            df = earn([underlying.upper()], days_ahead=dte_max)
            if df is not None and not df.empty:
                return (f"{underlying} has an earnings date within the {dte_max}-day "
                        f"option window; options rejected to avoid IV crush.")
            return ""
        except Exception as e:
            logger.warning(f"Earnings check failed for {underlying}: {e}. Allowing (fail-open).")
            return ""

