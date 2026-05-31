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

## Possible next steps

This repo currently holds the alert format + monitoring logs. It could be extended into a
scheduled/automated monitor (e.g., a script that polls news/Truth Social feeds on an
interval, dedupes mentions, enriches with live quotes, and appends new alerts). Ask if
you'd like that built.
