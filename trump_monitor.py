#!/usr/bin/env python3
"""
Trump Stock-Mention Monitor — scheduled sweep.

Pipeline: fetch candidate news (Google News RSS, zero-dep) -> drop already-seen
-> analyze (Claude API if ANTHROPIC_API_KEY is set; heuristic fallback otherwise)
-> enrich with live quotes (yfinance, optional) -> append dated alert file +
regenerate alerts/INDEX.md + persist the seen-store.

Designed to run unattended on a schedule (GitHub Actions or cron). It is
idempotent per day and dedupes mentions across runs and across sources, so most
runs produce few or zero new alerts.

Core path needs only the Python standard library. Optional extras:
  - anthropic   -> high-quality LLM analysis (set ANTHROPIC_API_KEY)
  - yfinance    -> live price enrichment

Usage:
  python trump_monitor.py                 # one sweep
  python trump_monitor.py --self-test     # offline pipeline check, writes to a temp dir
  python trump_monitor.py --dry-run       # analyze + print, write nothing
  python trump_monitor.py --lookback-days 3 --limit 60

Not financial advice. Alerts aggregate publicly reported statements with source
links; quotes are emitted only when present in the source text.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

MODEL_DEFAULT = "claude-opus-4-8"
USER_AGENT = "Mozilla/5.0 (compatible; trump-alerts-monitor/1.0)"

DEFAULT_CONFIG: dict[str, Any] = {
    # Search queries run against Google News RSS. Kept broad; the analyzer is
    # responsible for filtering down to genuine, specific company mentions.
    "queries": [
        'Trump company stock',
        'Trump "Truth Social" stock',
        'Trump praises company shares',
        'Trump tariff company shares',
        'Trump CEO comment stock',
    ],
    # Watchlist: name -> ticker. Used by the heuristic fallback to detect/label
    # mentions, and appended to the search queries ("Trump <company>").
    "watchlist": {
        "Micron": "MU",
        "Dell": "DELL",
        "Intel": "INTC",
        "Palantir": "PLTR",
        "Nvidia": "NVDA",
        "Apple": "AAPL",
        "Boeing": "BA",
        "Oracle": "ORCL",
        "Tesla": "TSLA",
        "Eli Lilly": "LLY",
        "Pfizer": "PFE",
        "Trump Media": "DJT",
    },
    "lookback_days": 2,           # only consider items published within N days
    "max_items": 50,              # cap candidate items per run (cost guard)
    "batch_size": 10,             # candidate items per analysis API call
    "fetch_article_body": False,  # best-effort full-text fetch (often paywalled)
    "enrich_prices": True,        # use yfinance if available
    "model": MODEL_DEFAULT,
    "effort": None,               # None -> API default ("high"); or low/medium/high/max
    "thinking": True,             # adaptive thinking on the analysis call
    "out_dir": "alerts",
    "seen_store": ".monitor_seen.json",
}

ENV_OVERRIDES = {
    "MONITOR_MODEL": ("model", str),
    "MONITOR_EFFORT": ("effort", str),
    "MONITOR_LOOKBACK_DAYS": ("lookback_days", int),
    "MONITOR_MAX_ITEMS": ("max_items", int),
    "MONITOR_BATCH_SIZE": ("batch_size", int),
    "MONITOR_OUT_DIR": ("out_dir", str),
}


def load_config(path: str | None) -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        cfg.update(user)
    for env, (key, cast) in ENV_OVERRIDES.items():
        if os.getenv(env):
            try:
                cfg[key] = cast(os.environ[env])
            except ValueError:
                pass
    return cfg


# --------------------------------------------------------------------------- #
# Source fetch — Google News RSS (no API key required)
# --------------------------------------------------------------------------- #

def _http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_pubdate(raw: str) -> dt.datetime | None:
    if not raw:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            d = dt.datetime.strptime(raw, fmt)
            return d.astimezone(dt.timezone.utc).replace(tzinfo=None) if d.tzinfo else d
        except ValueError:
            continue
    return None


def google_news_rss(query: str) -> list[dict[str, Any]]:
    """Fetch one Google News RSS search query. Returns [] on any failure."""
    params = urllib.parse.urlencode(
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )
    url = f"https://news.google.com/rss/search?{params}"
    try:
        raw = _http_get(url)
        root = ET.fromstring(raw)
    except Exception as exc:  # network, parse, etc. — non-fatal per query
        print(f"  [warn] query failed ({query!r}): {exc}", file=sys.stderr)
        return []

    items: list[dict[str, Any]] = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        source_el = it.find("source")
        source = source_el.text.strip() if source_el is not None and source_el.text else ""
        summary = _strip_html(it.findtext("description") or "")
        published = _parse_pubdate(it.findtext("pubDate") or "")
        if not title or not link:
            continue
        items.append({
            "id": guid or link,
            "title": title,
            "link": link,
            "source": source,
            "summary": summary,
            "published": published,
        })
    return items


def fetch_article_body(url: str) -> str:
    """Best-effort full text. Returns '' on failure (paywalls are common)."""
    try:
        raw = _http_get(url, timeout=15)
        text = _strip_html(raw.decode("utf-8", errors="ignore"))
        return text[:4000]
    except Exception:
        return ""


def fetch_candidates(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    queries = list(cfg["queries"])
    for name in cfg.get("watchlist", {}):
        queries.append(f"Trump {name}")

    seen_ids: set[str] = set()
    items: list[dict[str, Any]] = []
    for q in queries:
        for it in google_news_rss(q):
            if it["id"] in seen_ids:
                continue
            seen_ids.add(it["id"])
            items.append(it)
        time.sleep(0.3)  # be polite to the feed
    return items


def filter_candidates(
    items: list[dict[str, Any]], seen: set[str], lookback_days: int, limit: int
) -> list[dict[str, Any]]:
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=lookback_days)
    fresh: list[dict[str, Any]] = []
    for it in items:
        if it["id"] in seen:
            continue
        pub = it.get("published")
        if pub is not None and pub < cutoff:
            continue
        fresh.append(it)
    # Newest first when we have dates; cap to the cost guard.
    fresh.sort(key=lambda i: i.get("published") or dt.datetime.min, reverse=True)
    return fresh[:limit]


# --------------------------------------------------------------------------- #
# Seen-store (dedupe across runs)
# --------------------------------------------------------------------------- #

def load_seen(path: str) -> dict[str, set[str]]:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return {"items": set(data.get("items", [])), "alerts": set(data.get("alerts", []))}
        except Exception:
            pass
    return {"items": set(), "alerts": set()}


def save_seen(path: str, seen: dict[str, set[str]]) -> None:
    # Cap stored history so the file doesn't grow without bound.
    payload = {
        "items": sorted(seen["items"])[-5000:],
        "alerts": sorted(seen["alerts"])[-5000:],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def alert_key(alert: dict[str, Any]) -> str:
    company = (alert.get("company") or "").strip().lower()
    quote = (alert.get("exact_quote") or alert.get("why") or "").strip().lower()[:120]
    date = (alert.get("date") or "").strip()
    return hashlib.sha1(f"{company}|{quote}|{date}".encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Analysis — Claude API (preferred) with heuristic fallback
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """\
You are a financial-news analyst. You are given a batch of candidate news items \
(headline, source, date, URL, and a short summary or body excerpt). Your job is \
to identify items that report Donald Trump DIRECTLY mentioning a specific, \
publicly traded company in a way that could matter to investors, and to emit a \
structured alert for each qualifying mention.

QUALIFYING mention (emit an alert):
- Trump names a specific publicly traded company or its stock ticker, OR
- Trump names a CEO of a public company in a way that could affect that company, OR
- Trump announces a policy that clearly and materially affects a NAMED public company.

DO NOT emit an alert for:
- general political commentary or campaign rhetoric with no identifiable company,
- broad economic comments with no specific company,
- a company that is privately held (set ticker to null and lower confidence if unsure).

For each qualifying item produce one alert object:
- company: the company name.
- ticker: the stock ticker if you are confident; otherwise null.
- exact_quote: a direct quotation of Trump ONLY if a quotation actually appears in \
the provided text. If no direct quote is present, set this to null. Never invent or \
paraphrase a quote into this field.
- tone: "positive", "negative", or "neutral" (Trump's tone toward the company).
- priority: "HIGH" (a specific company/ticker is named), "MEDIUM" (an industry/policy \
that clearly affects a named company), or "LOW" (general economic comment that still \
names a company in passing).
- confidence: integer 0-100 — your confidence that this is a real, market-relevant, \
directly-attributable Trump mention given the provided text.
- why: one sentence on why it was mentioned.
- market_impact: one sentence on the likely market impact.
- beneficiaries: list of tickers that may benefit (may be empty).
- harmed: list of tickers that may be hurt (may be empty).
- material: boolean — is this likely material to investors?
- already_priced_in: short phrase on whether the market has likely already priced it in.
- source_url, source_name, date: copied from the provided item.

Strict rules:
- Use ONLY facts present in the provided text. Do not invent quotes, prices, deals, or events.
- If several items describe the SAME underlying mention, emit ONE alert and cite the best source.
- If nothing in the batch qualifies, return an empty alerts list.
- This is informational analysis, not financial advice.

Return your answer as JSON matching the provided schema.\
"""

ALERT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "alerts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "company": {"type": "string"},
                    "ticker": {"type": ["string", "null"]},
                    "exact_quote": {"type": ["string", "null"]},
                    "tone": {"type": "string", "enum": ["positive", "negative", "neutral"]},
                    "priority": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                    "confidence": {"type": "integer"},
                    "why": {"type": "string"},
                    "market_impact": {"type": "string"},
                    "beneficiaries": {"type": "array", "items": {"type": "string"}},
                    "harmed": {"type": "array", "items": {"type": "string"}},
                    "material": {"type": "boolean"},
                    "already_priced_in": {"type": "string"},
                    "source_url": {"type": "string"},
                    "source_name": {"type": "string"},
                    "date": {"type": "string"},
                },
                "required": [
                    "company", "ticker", "exact_quote", "tone", "priority",
                    "confidence", "why", "market_impact", "beneficiaries", "harmed",
                    "material", "already_priced_in", "source_url", "source_name", "date",
                ],
            },
        }
    },
    "required": ["alerts"],
}


def _render_items_for_prompt(items: list[dict[str, Any]], include_body: bool) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        pub = it.get("published")
        date_str = pub.strftime("%Y-%m-%d") if pub else "unknown"
        lines.append(f"--- ITEM {i} ---")
        lines.append(f"headline: {it['title']}")
        lines.append(f"source: {it.get('source') or 'unknown'}")
        lines.append(f"date: {date_str}")
        lines.append(f"url: {it['link']}")
        if it.get("summary"):
            lines.append(f"summary: {it['summary']}")
        if include_body and it.get("body"):
            lines.append(f"body_excerpt: {it['body']}")
        lines.append("")
    return "\n".join(lines)


def _extract_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {"alerts": []}


def analyze_with_claude(items: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    import anthropic  # lazy: only needed when a key is configured

    client = anthropic.Anthropic()
    run_date = dt.date.today().isoformat()
    alerts: list[dict[str, Any]] = []
    batch_size = max(1, int(cfg.get("batch_size", 10)))

    for start in range(0, len(items), batch_size):
        chunk = items[start:start + batch_size]
        user_text = (
            f"Run date: {run_date}\n\n"
            f"Analyze these {len(chunk)} candidate news items and return qualifying "
            f"Trump stock-mention alerts as JSON.\n\n"
            + _render_items_for_prompt(chunk, cfg.get("fetch_article_body", False))
        )
        # Stable instructions go in `system` (cached); volatile content (date +
        # articles) goes in the user turn, after the cache breakpoint.
        kwargs: dict[str, Any] = {
            "model": cfg.get("model", MODEL_DEFAULT),
            "max_tokens": 8000,
            "system": [{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": [{"role": "user", "content": user_text}],
        }
        if cfg.get("thinking", True):
            kwargs["thinking"] = {"type": "adaptive"}
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": ALERT_SCHEMA}
        }
        if cfg.get("effort"):
            output_config["effort"] = cfg["effort"]

        try:
            resp = client.messages.create(output_config=output_config, **kwargs)
        except TypeError:
            # Older SDK without output_config: ask for JSON in-prompt and parse.
            kwargs["messages"][0]["content"] += (
                "\n\nRespond with ONLY a JSON object: "
                '{"alerts": [ ... ]} matching the described fields.'
            )
            resp = client.messages.create(**kwargs)
        except anthropic.BadRequestError as exc:
            print(f"  [warn] analysis request rejected: {exc}", file=sys.stderr)
            continue

        if getattr(resp, "stop_reason", None) == "refusal":
            print("  [warn] model refused a batch; skipping it", file=sys.stderr)
            continue

        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        data = _extract_json(text)
        for a in data.get("alerts", []):
            if isinstance(a, dict) and a.get("company"):
                alerts.append(a)

        usage = getattr(resp, "usage", None)
        if usage is not None:
            print(
                f"  [claude] batch {start // batch_size + 1}: "
                f"{len(data.get('alerts', []))} alert(s); "
                f"cache_read={getattr(usage, 'cache_read_input_tokens', 0)} "
                f"in={getattr(usage, 'input_tokens', 0)} "
                f"out={getattr(usage, 'output_tokens', 0)}"
            )
    return alerts


def analyze_heuristic(items: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """No-API fallback: keyword-match the watchlist in headline/summary.

    Lower quality than the LLM path — it can't extract exact quotes or judge
    tone/materiality reliably — so confidence is capped and quotes are omitted.
    """
    watchlist: dict[str, str] = cfg.get("watchlist", {})
    alerts: list[dict[str, Any]] = []
    for it in items:
        blob = f"{it['title']} {it.get('summary', '')}"
        low = blob.lower()
        if "trump" not in low:
            continue
        for name, ticker in watchlist.items():
            if re.search(rf"\b{re.escape(name.lower())}\b", low):
                pub = it.get("published")
                alerts.append({
                    "company": name,
                    "ticker": ticker,
                    "exact_quote": None,  # not reliably extractable without the LLM
                    "tone": "neutral",
                    "priority": "HIGH",
                    "confidence": 45,
                    "why": "Heuristic keyword match (Trump + company) in headline/summary.",
                    "market_impact": "Review the source; heuristic mode does not assess impact.",
                    "beneficiaries": [],
                    "harmed": [],
                    "material": False,
                    "already_priced_in": "unknown (heuristic candidate)",
                    "source_url": it["link"],
                    "source_name": it.get("source") or "",
                    "date": pub.strftime("%Y-%m-%d") if pub else dt.date.today().isoformat(),
                })
                break  # one alert per item
    return alerts


# --------------------------------------------------------------------------- #
# Enrichment — live quotes (optional)
# --------------------------------------------------------------------------- #

def enrich_prices(alerts: list[dict[str, Any]]) -> None:
    tickers = sorted({a["ticker"] for a in alerts if a.get("ticker")})
    if not tickers:
        return
    try:
        import yfinance as yf
    except Exception:
        print("  [info] yfinance not installed; skipping price enrichment", file=sys.stderr)
        return
    for t in tickers:
        try:
            info = yf.Ticker(t).fast_info
            price = getattr(info, "last_price", None)
            prev = getattr(info, "previous_close", None)
            if price is None:
                continue
            chg = ""
            if prev:
                pct = (price - prev) / prev * 100
                chg = f" ({pct:+.1f}% vs prev close)"
            note = f"${price:,.2f}{chg}"
        except Exception:
            continue
        for a in alerts:
            if a.get("ticker") == t:
                a["price_note"] = note


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

PRIORITY_EMOJI = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}

DISCLAIMER = (
    "> **Disclaimer:** Auto-generated, informational only — **not financial advice.** "
    "Quotes are emitted only when present in the cited source; verify independently."
)


def render_alert(a: dict[str, Any]) -> str:
    pri = a.get("priority", "LOW")
    emoji = PRIORITY_EMOJI.get(pri, "🟢")
    ticker = a.get("ticker") or "—"
    quote = a.get("exact_quote")
    quote_line = f"> \"{quote}\"" if quote else "> _(no direct quote captured from source)_"
    benef = ", ".join(a.get("beneficiaries") or []) or "—"
    harmed = ", ".join(a.get("harmed") or []) or "—"
    price = f" · **Live:** {a['price_note']}" if a.get("price_note") else ""
    src = a.get("source_name") or "source"
    return "\n".join([
        f"### {emoji} {pri} — {a.get('company', 'Unknown')} ({ticker})",
        "",
        f"🚨 **STOCK MENTION ALERT**",
        "",
        f"- **Date:** {a.get('date', 'unknown')}",
        f"- **Source:** [{src}]({a.get('source_url', '')})",
        f"- **Ticker:** {ticker}{price}",
        "",
        "**Exact Quote:**",
        quote_line,
        "",
        f"**Context:** {a.get('why', '')} _Tone:_ {a.get('tone', 'neutral')}. "
        f"_Market impact:_ {a.get('market_impact', '')}",
        "",
        f"**Confidence:** {a.get('confidence', 0)}/100 · "
        f"**Material:** {'yes' if a.get('material') else 'no'} · "
        f"**Already priced in:** {a.get('already_priced_in', 'unknown')}",
        "",
        f"**May benefit:** {benef} · **May be hurt:** {harmed}",
        "",
        "---",
    ])


def write_report(alerts: list[dict[str, Any]], out_dir: str, run_dt: dt.datetime) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{run_dt.date().isoformat()}-auto.md")
    stamp = run_dt.strftime("%Y-%m-%d %H:%M UTC")

    new_file = not os.path.exists(path)
    blocks: list[str] = []
    if new_file:
        blocks.append(f"# 🚨 Trump Stock-Mention Alerts — {run_dt.date().isoformat()} (automated)\n")
        blocks.append(DISCLAIMER + "\n")
    blocks.append(f"## Sweep at {stamp} — {len(alerts)} new alert(s)\n")
    # HIGH first, then by confidence.
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    for a in sorted(alerts, key=lambda x: (order.get(x.get("priority", "LOW"), 3),
                                           -int(x.get("confidence", 0)))):
        blocks.append(render_alert(a))
        blocks.append("")

    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(blocks) + "\n")
    return path


def regenerate_index(out_dir: str) -> None:
    if not os.path.isdir(out_dir):
        return
    files = sorted(
        (f for f in os.listdir(out_dir) if f.endswith(".md") and f != "INDEX.md"),
        reverse=True,
    )
    lines = ["# Alert index", "", "| Date | File |", "|---|---|"]
    for f in files:
        lines.append(f"| {f[:10]} | [{f}](./{f}) |")
    with open(os.path.join(out_dir, "INDEX.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def run_sweep(cfg: dict[str, Any], dry_run: bool) -> int:
    has_key = bool(os.getenv("ANTHROPIC_API_KEY"))
    engine = "claude" if has_key else "heuristic"
    print(f"[monitor] engine={engine} model={cfg['model']} "
          f"lookback={cfg['lookback_days']}d limit={cfg['max_items']}")

    seen = load_seen(cfg["seen_store"])
    print("[monitor] fetching candidates ...")
    candidates = fetch_candidates(cfg)
    print(f"[monitor] {len(candidates)} candidate item(s) fetched")

    fresh = filter_candidates(candidates, seen["items"], cfg["lookback_days"], cfg["max_items"])
    print(f"[monitor] {len(fresh)} new, in-window item(s) to analyze")
    if not fresh:
        if not dry_run:
            regenerate_index(cfg["out_dir"])
        print("[monitor] nothing new; done")
        return 0

    if cfg.get("fetch_article_body"):
        for it in fresh:
            it["body"] = fetch_article_body(it["link"])

    if has_key:
        try:
            alerts = analyze_with_claude(fresh, cfg)
        except Exception as exc:
            print(f"[monitor] Claude analysis failed ({exc}); falling back to heuristic",
                  file=sys.stderr)
            alerts = analyze_heuristic(fresh, cfg)
    else:
        print("[monitor] no ANTHROPIC_API_KEY -> heuristic candidate mode")
        alerts = analyze_heuristic(fresh, cfg)

    # Dedupe alerts across runs.
    new_alerts = [a for a in alerts if alert_key(a) not in seen["alerts"]]
    print(f"[monitor] {len(alerts)} alert(s) found, {len(new_alerts)} new after dedupe")

    if cfg.get("enrich_prices", True) and new_alerts:
        enrich_prices(new_alerts)

    if dry_run:
        print(json.dumps(new_alerts, indent=2, default=str))
        return 0

    if new_alerts:
        path = write_report(new_alerts, cfg["out_dir"], dt.datetime.utcnow())
        print(f"[monitor] wrote {len(new_alerts)} alert(s) -> {path}")
        for a in new_alerts:
            seen["alerts"].add(alert_key(a))
        try:
            import notify
            sent = notify.notify(new_alerts)
            if sent:
                print(f"[monitor] notified: {', '.join(sent)}")
            elif notify.configured_channels():
                print("[monitor] notification channels configured but all sends failed")
        except Exception as exc:  # notifications must never break the sweep
            print(f"[monitor] notify step failed: {exc}", file=sys.stderr)

    for it in fresh:
        seen["items"].add(it["id"])
    save_seen(cfg["seen_store"], seen)
    regenerate_index(cfg["out_dir"])
    return 0


def self_test(cfg: dict[str, Any]) -> int:
    """Offline check of the analyze->dedupe->render->index path (no network/API)."""
    import tempfile
    print("[self-test] running offline pipeline check ...")
    sample = [{
        "id": "sample-1",
        "title": 'Trump says "Micron is great" at rally; chipmaker stock jumps',
        "link": "https://example.com/micron",
        "source": "Example Wire",
        "summary": 'President Trump praised Micron Technology, saying "Micron is great" during remarks.',
        "published": dt.datetime.utcnow(),
    }, {
        "id": "sample-2",
        "title": "Local council debates zoning rules",  # should NOT match
        "link": "https://example.com/zoning",
        "source": "Example Town News",
        "summary": "No company or market relevance here.",
        "published": dt.datetime.utcnow(),
    }]
    alerts = analyze_heuristic(sample, cfg)
    assert len(alerts) == 1, f"expected 1 heuristic alert, got {len(alerts)}"
    assert alerts[0]["ticker"] == "MU", alerts[0]
    key = alert_key(alerts[0])
    assert key == alert_key(alerts[0]), "alert_key not stable"

    tmp = tempfile.mkdtemp(prefix="trump-monitor-selftest-")
    path = write_report(alerts, tmp, dt.datetime.utcnow())
    regenerate_index(tmp)
    assert os.path.exists(path), "report not written"
    assert os.path.exists(os.path.join(tmp, "INDEX.md")), "index not written"
    body = open(path, encoding="utf-8").read()
    assert "STOCK MENTION ALERT" in body and "Micron" in body, "render missing content"
    print(f"[self-test] OK — sample report at {path}")
    print(open(path, encoding="utf-8").read())
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Trump stock-mention monitor (scheduled sweep).")
    p.add_argument("--config", default="monitor_config.json", help="path to config JSON")
    p.add_argument("--lookback-days", type=int, help="override lookback window")
    p.add_argument("--limit", type=int, help="override max candidate items")
    p.add_argument("--out-dir", help="override output directory")
    p.add_argument("--dry-run", action="store_true", help="analyze and print; write nothing")
    p.add_argument("--self-test", action="store_true", help="offline pipeline check")
    p.add_argument("--test-notify", action="store_true",
                   help="send a sample alert to configured notification channels")
    args = p.parse_args()

    cfg = load_config(args.config)
    if args.lookback_days is not None:
        cfg["lookback_days"] = args.lookback_days
    if args.limit is not None:
        cfg["max_items"] = args.limit
    if args.out_dir:
        cfg["out_dir"] = args.out_dir

    if args.self_test:
        return self_test(cfg)
    if args.test_notify:
        import notify
        return notify.test_notify()
    return run_sweep(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
