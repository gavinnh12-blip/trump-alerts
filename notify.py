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
import urllib.request
from typing import Any

PRIORITY_EMOJI = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}
_TIMEOUT = 15
# Discord/ntfy sit behind Cloudflare, which blocks urllib's default
# "Python-urllib/x.y" User-Agent with a 403. Send a descriptive UA instead.
_USER_AGENT = "Mozilla/5.0 (compatible; trump-alerts-monitor/1.0; +https://github.com/gavinnh12-blip/trump-alerts)"


def _post(url: str, data: bytes, headers: dict[str, str]) -> None:
    headers = {"User-Agent": _USER_AGENT, **headers}  # caller may override UA
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        resp.read()


def format_summary(alerts: list[dict[str, Any]], max_lines: int = 6) -> tuple[str, str]:
    """Return (title, body) — short enough for a phone push notification."""
    n = len(alerts)
    title = f"🚨 Trump stock alert ({n} new)" if n != 1 else "🚨 Trump stock alert"
    lines: list[str] = []
    for a in alerts[:max_lines]:
        emoji = PRIORITY_EMOJI.get(a.get("priority", "LOW"), "🟢")
        ticker = a.get("ticker") or "—"
        company = a.get("company", "Unknown")
        quote = a.get("exact_quote")
        snippet = f'"{quote}"' if quote else (a.get("why") or "").strip()
        if len(snippet) > 90:
            snippet = snippet[:87] + "…"
        conf = a.get("confidence", 0)
        price = f" · {a['price_note']}" if a.get("price_note") else ""
        lines.append(f"{emoji} {company} ({ticker}) — {snippet} · conf {conf}{price}")
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
        "priority": "HIGH", "confidence": 95, "why": "praised at a rally",
        "price_note": "$971.00 (+2.1% vs prev close)",
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


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Notification helper for the monitor.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--test", action="store_true", help="send a sample alert to configured channels")
    g.add_argument("--failure", metavar="RUN_URL", help="send a 'run failed' alert with this link")
    args = p.parse_args(argv)
    if args.failure:
        return notify_failure(args.failure)
    return test_notify()  # default / --test


if __name__ == "__main__":
    sys.exit(main())
