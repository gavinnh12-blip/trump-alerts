"""
Multi-channel notifications for the Trump stock-mention monitor.

Each channel turns on by setting its environment variable(s); unset channels are
silently skipped. All transport is stdlib urllib — no dependencies, no accounts
required for the easiest option (ntfy).

  Phone push (easiest): NTFY_TOPIC          [+ NTFY_SERVER, default https://ntfy.sh]
  Telegram (phone+chat): TELEGRAM_BOT_TOKEN  + TELEGRAM_CHAT_ID
  Slack (chat):          SLACK_WEBHOOK_URL
  Discord (chat):        DISCORD_WEBHOOK_URL

Send a real test to whatever you've configured:
  python trump_monitor.py --test-notify
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from typing import Any

PRIORITY_EMOJI = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}
STATEMENT_TAG = {
    "endorsement": "👍 ENDORSEMENT",
    "criticism": "👎 CRITICISM",
    "policy": "📜 POLICY",
    "mention": "💬 mention",
}
_TIMEOUT = 15
_RETRIES = 3            # transient blips (timeouts, 5xx, 429) self-heal
_BACKOFF = 1.5          # seconds, doubled each retry
# Discord/ntfy sit behind Cloudflare, which blocks urllib's default
# "Python-urllib/x.y" User-Agent with a 403. Send a descriptive UA instead.
_USER_AGENT = "Mozilla/5.0 (compatible; trump-alerts-monitor/1.0; +https://github.com/gavinnh12-blip/trump-alerts)"


def _retryable(exc: Exception) -> bool:
    """True for transient failures worth retrying (timeouts, 429, 5xx, conn drops)."""
    import urllib.error
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code < 600
    # URLError (DNS/conn reset), socket timeout, etc. — transient.
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


def _post(url: str, data: bytes, headers: dict[str, str]) -> None:
    headers = {"User-Agent": _USER_AGENT, **headers}  # caller may override UA
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                resp.read()
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == _RETRIES - 1 or not _retryable(exc):
                raise
            time.sleep(_BACKOFF * (2 ** attempt))
    if last:  # unreachable, but keeps type-checkers happy
        raise last


def signal_for(a: dict[str, Any]) -> str:
    """Map a statement to a directional sentiment signal (NOT financial advice).

    Endorsement / positive tone -> bullish (buy-side lean);
    criticism / negative tone   -> bearish (sell-side lean);
    otherwise neutral. This reflects the naive "Trump effect" direction his
    words imply — it is a sentiment signal, not a recommendation to trade.
    """
    stype = (a.get("statement_type") or "").lower()
    tone = (a.get("tone") or "").lower()
    if stype == "endorsement" or tone == "positive":
        return "🟢 BUY-side signal (bullish lean)"
    if stype == "criticism" or tone == "negative":
        return "🔴 SELL-side signal (bearish lean)"
    return "⚪ Neutral signal"


def format_summary(alerts: list[dict[str, Any]], max_lines: int = 6) -> tuple[str, str]:
    """Return (title, body) — short enough for a phone push notification."""
    n = len(alerts)
    title = f"🚨 Trump stock alert ({n} new)" if n != 1 else "🚨 Trump stock alert"
    lines: list[str] = []
    for a in alerts[:max_lines]:
        emoji = PRIORITY_EMOJI.get(a.get("priority", "LOW"), "🟢")
        ticker = a.get("ticker") or "—"
        company = a.get("company", "Unknown")
        tag = STATEMENT_TAG.get(a.get("statement_type", "mention"), "💬 mention")
        # Always show substance: verbatim quote if present, else key_info, else why.
        quote = a.get("exact_quote")
        if quote:
            snippet = f'"{quote}"'
        else:
            snippet = (a.get("key_info") or a.get("why") or "").strip()
        if len(snippet) > 110:
            snippet = snippet[:107] + "…"
        conf = a.get("confidence", 0)
        price = f" · {a['price_note']}" if a.get("price_note") else ""
        when = f" · said {a['date']}" if a.get("date") else ""
        lines.append(f"{emoji} {tag} — {company} ({ticker}): {snippet} · conf {conf}{when}{price}")
        lines.append(f"   {signal_for(a)} — not advice")
        url = a.get("source_url")
        if url:
            lines.append(f"   {url}")
    if n > max_lines:
        lines.append(f"…and {n - max_lines} more (see the repo's alerts/ folder)")
    return title, "\n".join(lines)


# --------------------------------------------------------------------------- #
# Channels
# --------------------------------------------------------------------------- #

def send_ntfy(title: str, body: str) -> None:
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        return
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {
        "Title": title.encode("ascii", "ignore").decode() or "Trump stock alert",
        "Priority": "high",
        "Tags": "rotating_light,chart_with_upwards_trend",
    }
    token = os.getenv("NTFY_TOKEN")  # only needed for protected topics
    if token:
        headers["Authorization"] = f"Bearer {token}"
    _post(f"{server}/{topic}", body.encode("utf-8"), headers)


def send_telegram(title: str, body: str) -> None:
    tok = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    payload = json.dumps({
        "chat_id": chat,
        "text": f"{title}\n\n{body}",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    _post(
        f"https://api.telegram.org/bot{tok}/sendMessage",
        payload,
        {"Content-Type": "application/json"},
    )


def send_slack(title: str, body: str) -> None:
    url = os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        return
    payload = json.dumps({"text": f"*{title}*\n{body}"}).encode("utf-8")
    _post(url, payload, {"Content-Type": "application/json"})


def send_discord(title: str, body: str) -> None:
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        return
    content = f"**{title}**\n{body}"[:1900]  # Discord hard-caps at 2000 chars
    payload = json.dumps({"content": content}).encode("utf-8")
    _post(url, payload, {"Content-Type": "application/json"})


_CHANNELS = [
    ("ntfy", "NTFY_TOPIC", send_ntfy),
    ("telegram", "TELEGRAM_BOT_TOKEN", send_telegram),
    ("slack", "SLACK_WEBHOOK_URL", send_slack),
    ("discord", "DISCORD_WEBHOOK_URL", send_discord),
]


def configured_channels() -> list[str]:
    return [name for name, env, _ in _CHANNELS if os.getenv(env)]


def notify(alerts: list[dict[str, Any]]) -> list[str]:
    """Push a summary of `alerts` to every configured channel. Best-effort:
    a failure in one channel never blocks the others or the sweep."""
    if not alerts:
        return []
    title, body = format_summary(alerts)
    sent: list[str] = []
    for name, env, fn in _CHANNELS:
        if not os.getenv(env):
            continue
        try:
            fn(title, body)
            sent.append(name)
        except Exception as exc:  # noqa: BLE001 — never let notify crash the run
            print(f"  [warn] {name} notification failed: {exc}", file=sys.stderr)
    return sent


def test_notify() -> int:
    """Send a sample alert to all configured channels and report what fired."""
    sample = [{
        "company": "Micron", "ticker": "MU", "exact_quote": "Micron is great",
        "statement_type": "endorsement", "source_type": "rally",
        "key_info": "Trump praised Micron at a rally as US chip manufacturing ramps up.",
        "priority": "HIGH", "confidence": 95, "why": "praised at a rally",
        "date": "2026-05-22", "price_note": "$971.00 (+2.1% vs prev close)",
        "source_url": "https://example.com/micron",
    }]
    title, body = format_summary(sample)
    print("Message that would be sent:\n")
    print(f"{title}\n{body}\n")
    channels = configured_channels()
    if not channels:
        print("No channels configured. Set one of these (env vars / repo secrets):")
        print("  NTFY_TOPIC               (phone push — easiest, no account)")
        print("  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID")
        print("  SLACK_WEBHOOK_URL")
        print("  DISCORD_WEBHOOK_URL")
        return 1
    print(f"Configured channels: {', '.join(channels)} — sending test ...")
    sent = notify(sample)
    if sent:
        print(f"Sent to: {', '.join(sent)} ✅")
        return 0
    print("Configured, but all sends failed — check the warnings above.", file=sys.stderr)
    return 2


def notify_failure(run_url: str) -> int:
    """Tell every configured channel that a monitor run failed. Best-effort.

    Invoked by the workflow's `if: failure()` step so a broken sweep never fails
    silently. Never raises — a notification problem must not mask the real error.
    """
    title = "⚠️ Trump monitor run FAILED"
    body = (
        "A scheduled sweep did not complete. Open the run log to see why:\n"
        f"{run_url}"
    )
    if not configured_channels():
        print("[notify] run failed but no channels configured; nothing to send")
        return 0
    sent: list[str] = []
    for name, env, fn in _CHANNELS:
        if not os.getenv(env):
            continue
        try:
            fn(title, body)
            sent.append(name)
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] {name} failure-alert failed: {exc}", file=sys.stderr)
    print(f"[notify] failure alert sent to: {', '.join(sent) or '(none succeeded)'}")
    return 0


def _count_alerts_today(out_dir: str = "alerts") -> int:
    """Count alert blocks written to today's auto file (best-effort, 0 on any issue)."""
    import datetime
    path = os.path.join(out_dir, f"{datetime.date.today().isoformat()}-auto.md")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().count("STOCK MENTION ALERT")
    except Exception:
        return 0


def heartbeat(out_dir: str = "alerts") -> int:
    """Send a daily 'still alive' note so silence never feels like a broken monitor.

    Best-effort and never fatal — a heartbeat hiccup must not fail the workflow.
    """
    import datetime
    n = _count_alerts_today(out_dir)
    today = datetime.date.today().isoformat()
    title = "✅ Trump monitor — daily check-in"
    body = (
        f"Monitor is running. {n} alert(s) so far today ({today}).\n"
        "No news means no message until Trump names a public company."
    )
    if not configured_channels():
        print("[notify] heartbeat: no channels configured; nothing to send")
        return 0
    sent: list[str] = []
    for name, env, fn in _CHANNELS:
        if not os.getenv(env):
            continue
        try:
            fn(title, body)
            sent.append(name)
        except Exception as exc:  # noqa: BLE001 — heartbeat must never break a run
            print(f"  [warn] {name} heartbeat failed: {exc}", file=sys.stderr)
    print(f"[notify] heartbeat sent to: {', '.join(sent) or '(none succeeded)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Notification helper for the monitor.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--test", action="store_true", help="send a sample alert to configured channels")
    g.add_argument("--failure", metavar="RUN_URL", help="send a 'run failed' alert with this link")
    g.add_argument("--heartbeat", action="store_true", help="send a daily 'monitor alive' check-in")
    p.add_argument("--out-dir", default="alerts", help="alerts dir (for --heartbeat counting)")
    args = p.parse_args(argv)
    if args.failure:
        return notify_failure(args.failure)
    if args.heartbeat:
        return heartbeat(args.out_dir)
    return test_notify()  # default / --test


if __name__ == "__main__":
    sys.exit(main())
