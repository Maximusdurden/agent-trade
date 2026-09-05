# Dexter Blog on agent-trade (Treat Motivated Capital)

This document describes the blog publishing layer that was **ported from
`dexter-trader`** into agent-trade. It explains how the daily post is generated
and published, and — critically — **how you can swap in your own persona** to
have the system write daily reports in *any* voice you want.

> TL;DR — the blog voice is data, not code. Everything about *how* it writes is
> a persona string in `core/personas.py`. Change `BLOG_PERSONA` (or add your own
> persona) and the next post is written in that voice, no code edits.

---

## 1. What the blog layer does

The blog publisher is a **read-only reporter** that runs separately from the
trading agent. It:

1. Pulls the fresh `trading_agent.db` from GCS (the same DB the strategy job uploads).
2. Bridges the agent-trade round-trips into the `realized_trades` mirror the
   blog's report code understands (`tools/build_blog_db.py`).
3. Grades closed trades (D-TAG letter grades) so posts carry a grade badge.
4. Assembles the WordPress post: market intro, per-ticker blurbs, a performance
   card image, and a yearly calendar.
5. Publishes the post, updates the sidebar widget + performance calendar page,
   and notifies Discord.

**It never trades.** It is invoked as `run_blog.py` (Cloud Run job) or
`python -m tools.blog_update` (locally), never as part of the trading loop.

### Module map

| File | Purpose |
|------|---------|
| `run_blog.py` | Cloud Run job entrypoint → `tools.blog_update.main()`. |
| `tools/blog_update.py` | Orchestrator: pull DB → bridge → grade → intro/blurbs → SEO → publish → sidebar → notify. Flags: `--local-db`, `--dry`, `--date`. |
| `tools/build_blog_db.py` | Bridge/adapter: `round_trip_to_row()` maps agent-trade `compute_closed_round_trips()` → dexter `realized_trades` schema. Upserts on `(ticker, entry_date, exit_date)` to preserve grade `id`s. |
| `core/wordpress.py` | WordPress REST helpers: auth, tags, auto-linking tickers/financial terms, disclaimer, image upload, sidebar widget, performance calendar, publish, JSON-LD schema, Discord notify. |
| `core/brain.py` | Persona-swappable LLM layer (intro + ticker blurbs) via `SharedLLMClient` (OpenRouter primary, Gemini fallback). Reads `BLOG_PERSONA`. |
| `core/personas.py` | **The voice registry.** Every persona is a system prompt string in `PERSONAS`. |
| `core/cards.py` | `render_performance_card` (Dexter 400×600 card), `generate_yearly_calendar_html`, `render_sidebar_image`. |
| `core/grader.py` | D-TAG ported trade grading → letter grades for posts. |
| `core/blog_stats.py` | Derive dashboard/round-trip stats into the dexter `DataFrame` + PnL helpers. |
| `core/seo.py` | `SEOAuditor` + `generate_seo_metadata` for on-page SEO on each post. |
| `core/config.py` | `BLOG_PERSONA`, `BLOG_MODEL`, `BLOG_MAX_OUTPUT_TOKENS`, `WP_*`, `DEXTER_LOGO_URL`, `AUTHOR_NAME`. |

---

## 2. How a persona works

Every persona is just a **system prompt string** stored in `core/personas.py`.
The active one is chosen by `core.config.BLOG_PERSONA` (an env var / Secret
Manager value), so **voice is a runtime setting, not code**.

Built-in personas:

| Key | Description |
|-----|-------------|
| `dexter` | **Default.** The 7-year-old brindle Bull Boxer who runs Treat Motivated Capital. Deadpan, dramatic, no dog puns, sparse realistic typos. |
| `oracle` | Cryptic all-knowing market seer; PnL as cosmic portent. |
| `rookie` | Nervous first-day intern who over-explains but stays technically correct. |
| `pirate` | Captain Blackcandle — an aggressive algorithmic pirate (legacy). |
| `derrick` | Lead dev / systems architect — concise, technical, emotionless (legacy). |

Each prompt enforces the same structural contract so the parser can always read
the output:

- **Blog intro:** 150–250 words covering market drivers and total PnL.
- **Ticker blurbs:** 2–3 sentences max per ticker (entries, exits, hold time, logic).
- **No markdown bolding (`**`)** in final output — clean plain-text paragraphs.

> **Branding rule (hard):** Personas may *only* describe how to write. They must
> **never** reference internal systems ("agent-trade", "the strategy", repo or
> GCP names). Readers only ever see Treat Motivated Capital + the persona voice.

---

## 3. Creating your own persona ("your own Dexter")

To make the system write daily reports in a brand-new voice:

### Step 1 — Add a persona to `core/personas.py`

Add a new key to the `PERSONAS` dict, e.g.:

```python
PERSONAS = {
    # existing personas...
    "captain_log": """
IDENTITY:
You are Captain Log, a retired starship fleet captain who now narrates daily
market movements for an adrift trading vessel. Steady, wry, occasionally
sentimental, deeply fond of your crew (the portfolio).

CORE TRAITS:
- Frame every trading day as a voyage: entries are dockings, exits are
  departures, PnL is the ship's manifest.
- Speak in calm, clipped captain's-orders cadence.
- Keep a running tally of "crew morale" = equity.

RULES:
1. Never reference internal systems; you run a trading vessel, that's all.
2. No markdown bolding (**). Clean plain text.
3. Blog intro: 150-250 words. Ticker blurbs: 2-3 sentences max.
    """,
}
```

### Step 2 — Select the persona at runtime

Set `BLOG_PERSONA=captain_log` in your environment (local `.env`, or the
`BLOG_PERSONA` env var on the blog Cloud Run job). No restart of code is needed —
just the next run uses the new voice.

```powershell
# local
> python -m tools.blog_update --dry --local-db   # still dexter by default
> $env:BLOG_PERSONA="captain_log"; python -m tools.blog_update --dry --local-db
```

### Step 3 — Tune the model (optional)

By default blog prose uses Hermes 3 Llama 3.1 70B via OpenRouter
(`BLOG_MODEL`). You can point it at any OpenRouter creative model, or add a
fallback in `core/brain.py`'s `OPENROUTER_MODEL_LIST`.

---

## 4. Running the blog publisher

### Locally (dry-run — builds + grades + prints, does NOT publish)

```powershell
> python -m tools.blog_update --dry --local-db
```

### Locally (real publish against your local DB)

```powershell
> python -m tools.blog_update --local-db
```

### Pulling fresh data from GCS first

Without `--local-db`, the orchestrator pulls `trading_agent.db` from GCS first
and **aborts rather than publish stale numbers** if the pull fails.

```powershell
> python -m tools.blog_update             # pull DB, bridge, grade, publish
> python -m tools.blog_update --date 2026-09-04   # target a specific trade date
```

### As a Cloud Run job (production)

`deploy/deploy_blog.ps1` builds the blog image (`run_blog.py` entrypoint),
creates/updates the `dexter-blog-update` Cloud Run **job**, and registers a
**Cloud Scheduler** trigger to run it after each trading day's strategy job
uploads its DB. Secrets (`WP_USER`, `WP_APP_PASSWORD`, `GEMINI_API_KEY`,
`OPENROUTER_API_KEY`) are injected from **Secret Manager**, never baked into the
image.

```powershell
> ./deploy/deploy_blog.ps1
```

> **Important:** the scheduler cron must run *after* the strategy job pushes its
> DB to GCS each trading day (see the placeholder `--schedule` in the script).

---

## 5. Configuration reference

All settings are read from env / Secret Manager in `core/config.py`:

| Env / Secret | Default | Meaning |
|--------------|---------|---------|
| `BLOG_PERSONA` | `dexter` | Active voice key in `core/personas.py`. |
| `BLOG_MODEL` | `nousresearch/hermes-3-llama-3.1-70b` | Primary OpenRouter creative model. |
| `BLOG_MAX_OUTPUT_TOKENS` | `1024` | Max tokens for intro + blurbs. |
| `WP_URL` | `https://treatmotivated.capital` | WordPress base URL. |
| `WP_USER` | — | WordPress username (Secret Manager in prod). |
| `WP_APP_PASSWORD` | — | WordPress Application Password (Secret Manager in prod). |
| `DEXTER_LOGO_URL` | — | Logo image used in cards/PDFs/emails. |
| `AUTHOR_NAME` | — | Byline shown in the post footer. |
| `WP_SIDEBAR_WIDGET_TITLE` | `Dexter Sidebar Widget` | Must match the WP widget title. |
| `WP_PERFORMANCE_PAGE_TITLE` | `Trading Performance` | Must match the WP calendar page title. |
| `GEMINI_API_KEY`, `OPENROUTER_API_KEY` | — | LLM keys (Secret Manager in prod). |
| `DISCORD_WEBHOOK_URL` | — | Webhook for "new post published" notifications. |

---

## 6. Related

- `reports/draft_strategy_change_post.md` — the live notice post announcing the
  move from dexter-trader to agent-trade.
- `tools/publish_strategy_change.py` — one-off post publisher used for the notice.
- `tools/liquidate_account.py` — dry-run-by-default account liquidation tool.
- `tests/test_blog_smoke.py` — offline smoke tests for the blog layer (bridge
  id-stability, autolink, disclaimer, SEO JSON extractor, letter grade, stats).