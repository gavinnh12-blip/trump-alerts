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

### Turn it on

**1. Try it now — zero setup.** On GitHub: **Actions** tab → **trump-stock-monitor** →
**Run workflow**. It runs immediately in free heuristic mode and commits any alerts it finds.

**2. Make it recurring.** Merge this branch into your default branch (`main`). The
every-3-hours sweep then runs on its own — GitHub only runs scheduled workflows from the
default branch, so nothing recurring happens until it's merged.

**3. Upgrade the analysis (optional).** Settings → **Secrets and variables → Actions** →
**New repository secret** → name it `ANTHROPIC_API_KEY`. With it, sweeps use Claude
(`claude-opus-4-8`); without it they still run, just with lighter heuristic analysis.

That's the whole setup — everything below is detail.

### Phone & chat alerts (optional)

By default, alerts are written to the [`alerts/`](./alerts/) folder. To **also** get pinged
on your phone or in a chat app the moment a new alert fires, add **one repo secret** for the
channel you use (Settings → Secrets and variables → Actions → New repository secret). Set any
combination; unset channels are skipped.

| You want… | Add secret(s) | How to get the value |
|---|---|---|
| **📱 Phone push** (easiest, no account) | `NTFY_TOPIC` | Install the **ntfy** app (iOS/Android), pick any hard-to-guess topic name (e.g. `trump-alerts-7h3k9`), **Subscribe** to it in the app, then use that same name as the secret. |
| **💬 Telegram** (phone + chat) | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Message **@BotFather** → `/newbot` → it gives you the token. Then message your new bot once, open `https://api.telegram.org/bot<TOKEN>/getUpdates`, and copy the `chat.id`. |
| **💬 Slack** | `SLACK_WEBHOOK_URL` | Slack → create an **Incoming Webhook** for a channel; paste the URL. |
| **💬 Discord** | `DISCORD_WEBHOOK_URL` | Channel → **Edit → Integrations → Webhooks → New Webhook → Copy URL**. |

**Test it without waiting for real news** (works locally or from the Actions "Run workflow" button):

```bash
NTFY_TOPIC=trump-alerts-7h3k9 python trump_monitor.py --test-notify
```

It prints the exact message it would send and delivers a sample alert to every configured
channel. Notifications are best-effort — a channel failing never blocks the sweep or the others.

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

### Notifications

Alert messages include the **date Trump made the statement** (`said YYYY-MM-DD`). On top of
new-alert pings and the run-failure alert, a **daily heartbeat** ("monitor alive, N alerts
today") fires once a day (13:00 UTC) so silence never feels like a broken monitor. Delivery
is resilient — transient blips (timeouts, 429, 5xx) are retried with backoff, and heartbeat
sends never fail the run.

### Run it locally

```bash
python trump_monitor.py                 # one sweep
python trump_monitor.py --self-test     # offline pipeline check (no network/key)
python trump_monitor.py --dry-run       # analyze + print, write nothing
python trump_monitor.py --test-notify   # send a sample alert to configured channels
python trump_monitor.py --lookback-days 3 --limit 60
```

Config lives in [`monitor_config.json`](./monitor_config.json) (watchlist, queries,
model, effort, batch size). Most knobs also accept env overrides (`MONITOR_MODEL`,
`MONITOR_EFFORT`, `MONITOR_LOOKBACK_DAYS`, …).

### Customize the schedule

- **Cadence:** edit the `cron` in
  [`.github/workflows/monitor.yml`](./.github/workflows/monitor.yml) (default: every 3 hours).
- **Run on your own server instead** (cron):
  ```cron
  0 */3 * * *  cd /path/to/trump-alerts && /usr/bin/python3 trump_monitor.py >> monitor.log 2>&1
  ```
