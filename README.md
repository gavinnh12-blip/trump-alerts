# trump-alerts

A monitoring log that flags when **Donald Trump directly mentions a publicly traded
company, ticker, CEO, or market-moving policy** — with an investor-focused alert for each.

> **Disclaimer:** Informational only — **not financial, investment, or legal advice.**
> Alerts aggregate publicly reported statements and news with source links; verify
> independently before making any decision. Any references to market manipulation,
> insider trading, or conflicts of interest are **reported allegations/concerns and are
> not proven**.

## What gets an alert

A specific, identifiable, market-relevant mention. We alert only when:
- a **specific company** is named,
- a **ticker** or a **publicly traded entity** is clearly referenced, or
- a **policy announcement** could materially affect a sector / named companies.

We ignore: general political commentary, campaign rhetoric with no identifiable market
impact, and duplicate mentions already reported.

## Priority levels

- 🔴 **HIGH** — direct mention of a company or ticker.
- 🟡 **MEDIUM** — discussion of an industry/policy likely to affect specific companies.
- 🟢 **LOW** — general economic comments / self-referential.

## Alert format

```
🚨 STOCK MENTION ALERT
Date / Time / Source / Link
Mentioned Company / Ticker
Exact Quote: "…"
Context Summary: why mentioned · tone (pos/neg/neutral) · potential market impact
Confidence Score: 0–100
Additional Analysis: material? · who benefits · who's hurt · historical parallels · already priced in?
Priority Level
```

## Monitoring runs

Each run is saved under [`alerts/`](./alerts/) as a dated Markdown file.

| Date | File |
|---|---|
| 2026-05-31 | [`alerts/2026-05-31-monitoring-run.md`](./alerts/2026-05-31-monitoring-run.md) |

## Method & data sources

- **Source sweep:** web search across speeches, rallies, press conferences, interviews,
  White House releases, Truth Social posts, and news reporting; cross-referenced for
  direct company mentions. Exact quotes are tied to a citable source — **nothing is
  fabricated**; if a primary source can't be confirmed, the item is labeled accordingly.
- **Market data:** a financial-data MCP server (Financial Modeling Prep) is connected for
  live quotes/fundamentals. Note: the real-time quote endpoints currently require a
  **premium FMP plan**, so prices in the 2026-05-31 run are sourced from cited news and
  labeled "as reported." Upgrading the FMP plan would enable live quote enrichment.

## Automated monitor

[`trump_monitor.py`](./trump_monitor.py) runs the whole pipeline on a schedule:

**fetch** candidate news (Google News RSS) → **drop already-seen** → **analyze**
→ **enrich** with live quotes → **append** a dated alert file + regenerate
[`alerts/INDEX.md`](./alerts/) + persist the dedupe store. It's idempotent per day
and dedupes mentions across runs and sources, so most sweeps produce few or zero new alerts.

### Analysis engine (graceful degradation)

- **With `ANTHROPIC_API_KEY` set** → Claude (`claude-opus-4-8` by default) evaluates each
  candidate and returns structured alerts (exact quote *only when present in the source*,
  tone, priority, confidence, materiality, beneficiaries/harmed). Uses the official
  `anthropic` SDK with `output_config.format` structured outputs, prompt caching on the
  (stable) system instructions, and adaptive thinking.
- **Without a key** → a heuristic candidate mode (watchlist keyword match) still runs for
  free; it flags candidates at lower confidence and can't extract quotes.

The core sweep needs only the **Python standard library**; `anthropic` and `yfinance`
([`requirements.txt`](./requirements.txt)) are optional extras.

### Run it

```bash
python trump_monitor.py                 # one sweep
python trump_monitor.py --self-test     # offline pipeline check (no network/key)
python trump_monitor.py --dry-run       # analyze + print, write nothing
python trump_monitor.py --lookback-days 3 --limit 60
```

Config lives in [`monitor_config.json`](./monitor_config.json) (watchlist, queries,
model, effort, batch size). Most knobs also accept env overrides (`MONITOR_MODEL`,
`MONITOR_EFFORT`, `MONITOR_LOOKBACK_DAYS`, …).

### Schedule it

- **GitHub Actions (recommended):** [`.github/workflows/monitor.yml`](./.github/workflows/monitor.yml)
  runs every 3 hours and commits new alerts back to the repo. Add an `ANTHROPIC_API_KEY`
  repo secret to enable LLM analysis (it falls back to heuristic mode without one); tune the
  `cron` to taste. Also runnable on demand from the Actions tab.
- **cron (local/server):**
  ```cron
  0 */3 * * *  cd /path/to/trump-alerts && /usr/bin/python3 trump_monitor.py >> monitor.log 2>&1
  ```
