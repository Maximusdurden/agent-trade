# Equity Desk — "Do NOT Do X" Playbook

> **Intended audience:** the autonomous agents doing the work — the **AI Screener**,
> the **MetaStrategist** (writes per-ticker strategy rules), and the **LLM brain**
> (appraises indicators against rules). This is derived from hard realized-PnL
> forensics on the equity desk, 2026-07-07 → 2026-08-31 (source of truth:
> `cloud_downloaded_trading_agent.db`). Feed the relevant sections into their
> system prompts / config so they stop repeating these root causes.

## Executive summary (one line each)
1. **Never trade a symbol the screener didn't pick.** Untracked names lost **-$540** (58 RTs, 8.6% win); watched names **gained +$180** (41.2% win). The fallback path is the #1 loss source.
2. **Never add (scale-in) to a losing position.** MS lost **-$226** by buying a falling knife all day on 8/06.
3. **Never "buy momentum" on a half-percent VWAP blip.** KO lost **-$128** buying +0.1–0.8% VWAP strength on noise, 0% win on 32 round-trips.

---

## DO NOT DO — Screener & Universe

- **Do not allow any entry in a symbol that is not in the current watchlist** (or an explicit, persisted whitelist).
  - *Evidence:* SPY, INTC, AMD, QQQ appear in the watchlist **0 of 3,911 times** yet still got traded and lost -$540 total (43 SPY + 3 INTC + 5 AMD + 7 QQQ round-trips). TSLA (0.2%) and GOOG (0.1%) bled too.
  - *Why:* the fallback/rule-based path bypasses the screener's whole purpose. Every one of these was a trade the screener explicitly declined to endorse.
- **Do not run a fallback "buy the oversold" leg outside the screened universe.** If you must act on an untracked symbol, the only valid actions are **SELL or HOLD**.
  - *Evidence:* 8.6% win rate on unwatched names vs 41.2% watched. The oversold fallback buys were pure drift/momentum-catch.

## DO NOT DO — MetaStrategist (rule writer)

- **Do not write "buy-the-dip / scale-in-on-weakness" rules without a single-entry cap.**
  - *Evidence:* MS rule (8/05) — "buy when vwap_dist_pct between **-1.5% and -0.5%**" → the brain bought **18 times on 8/06** while price sagged, then capitulated at the monthly low 8/20. 29 RTs, -$226, 17% win.
  - *Rule fix hint:* a dip-buy rule should buy **once**, never re-add below entry. Require an **RSI > 38 confirmation** and a hard **stop below the last add**, not an open-ended accumulation.
- **Do not write momentum rules whose entry signal is inside the ticker's noise band.**
  - *Evidence:* KO rule (8/07) — "buy when vwap_dist_pct **0.1%–0.8%**" → fired on noise, 0% win over 32 round-trips (-$128). The entry threshold is half a percent of VWAP — that is noise for KO.
  - *Rule fix hint:* momentum entries need a stronger signal (vwap_dist **≥ 1.5%** or **RSI > 55** confirmation) and a stop **≥ 2%** outside the noise band. A rule hitting **< 30% realized win after 5+ round-trips is broken — rewrite it**.
- **Do not let a rule go uncorrected once it demonstrably fails.** The MetaStrategist *does* sharpen winning rules intraday (MSFT support threshold ratcheted 0.6→1.5%; NVDA/XOM tightened), but MS and KO rules were never rewritten. Add a **feedback trigger: any rule with realized win% < 30% triggers a rewrite**.

## DO NOT DO — LLM brain (decision / indicator layer)

- **Do not accumulate a position below entry.** Adding size to a losing long is the MS loss pattern. Re-appraise each cycle independently; a dip-buy that failed the first time does not become more attractive.
- **Do not treat a sub-1% VWAP blip as a momentum signal.** The KO entries were +0.1–0.5% VWAP with RSI 46–62 — that is sideways noise, not strength. Require either RSI agreement or a bigger VWAP cross.
- **Do not hold untracked names for weeks.** INTC round-trips averaged **384h (16 days)** and all exited near lows. If a position isn't in the active universe, it needs a hard time/stop exit, not an indefinite hold.

## DO — keep doing these (proven winners)

| What | Why it works |
|---|---|
| Watch the screener universe | Watched names +41.2% win vs 8.6% unwatched |
| Require pullback-to-support **+ RSI/MACD gate** | XOM (79% win): "RSI<40 + MACD bullish" |
| Escalate entry threshold on strength | MSFT (100% win): support-hold + VWAP cross, threshold raised intraday |
| Trim winners into strength | NVDA (88.5% win) |

---

## One-glance loss ledger (what NOT to repeat → attribution)

| Ticker | Loss | Root cause | Action |
|---|---|---|---|
| SPY | -$415 | Fallback drift on untracked index, 2.3% win | Univerase guardrail |
| INTC | -$395 | Fallback legacy hold (384h), dumped at lows | Universe guardrail + max-hold |
| MS | -$226 | Dip-add scale-in on falling knife | Single-entry cap + RSI gate |
| KO | -$128 | Noise-band momentum buy, 0% win | Stronger signal + wider stop, or deactivate |
| GOOG/TSLA | -$90 | Untracked names, sub-4h whipsaw | Universe guardrail + min-hold |

If the agents deploy the three top guards (universe-only entries, no-scale-in, no-noise-momentum),
the equity desk's **-$1,600 of avoidable losses** (combining 7/7+ window with the excluded
7/6 crash attributed to the same fallback cause) is removed and the watched-universe edge (±$180
on 41% win, plus the strong MSFT/NVDA/XOM rules) is preserved.