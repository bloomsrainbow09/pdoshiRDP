"""Sample emails: prove the whole chain works without waiting for Monday.

The Indian market trades 09:15–15:30 IST on weekdays. Outside that, a system that is
working perfectly and a system that died three hours ago produce exactly the same empty
inbox. That is an unacceptable amount of time to spend not knowing.

So: real archived messages, replayed through the LIVE pipeline — the same guard, router,
sub-agents and templates that handle production — and delivered as real email. Nothing
here is a mock. If a sample arrives and reads correctly, the chain works end to end.

Three rules keep samples from becoming a second problem:

  * every subject is prefixed `[SAMPLE]`, and every body carries a footer naming the
    date of the message being replayed, so a sample can never be mistaken for a live
    alert or acted on as one
  * samples go through the same rate limit and the same dedupe as everything else
  * the auto-sample fires only when the market is CLOSED and nothing live has arrived
    for two hours — it is a proof of life, not a scheduled newsletter
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

AGENT = Path(__file__).resolve().parent
for p in (AGENT, AGENT.parent, AGENT / "delivery", AGENT / "templates",
          AGENT / "subagents"):
    sys.path.insert(0, str(p))

import httpx                                    # noqa: E402
import db as adb                                # noqa: E402
from core.delivery import policy  # noqa: E402

from core import resilience  # noqa: E402  explicit: a vertical may have one too
import subagents                                # noqa: E402
from core import active           # noqa: E402
from engine import db as edb                    # noqa: E402
from notifier import Message, default as default_notifier  # noqa: E402

def _render():
    """The ACTIVE vertical's renderer. There is no top-level `render` module: R5 moved
    the templates into the vertical and R7 removed the compatibility shim."""
    from core import active
    return active.module("render")


BAND = -987_000_000
MSGS = active.path() / "messages"
CACHE = AGENT.parent / "bench" / "sample_sources.json"

QUIET_FOR_S = 2 * 3600          # nothing live for two hours

PREFIX = "[SAMPLE] "


def _fixtures():
    """The active vertical's sample fixtures, or None if it has none.

    R7 moved WANTED, _NAMED and the market-hours window out of here: which archived
    message demonstrates which template is niche knowledge, and this module is the
    mechanism around it.
    """
    try:
        return active.module("samples")
    except FileNotFoundError:
        return None


def market_open(at=None) -> bool:
    """Delegates to the vertical. A vertical with no trading day is always open."""
    f = _fixtures()
    return f.market_open(at) if f and hasattr(f, "market_open") else True


def _wanted() -> dict:
    f = _fixtures()
    return getattr(f, "WANTED", {}) if f else {}


def _named():
    f = _fixtures()
    return getattr(f, "_NAMED", None) if f else None






def seconds_since_live_message() -> float | None:
    """How long since a real channel message arrived. None if there has never been one."""
    r = edb.fetch_one(
        "SELECT extract(epoch from (now() - max(received_at))) age "
        "FROM content.agent_events WHERE " + resilience.NOT_TEST)
    return float(r["age"]) if r and r["age"] is not None else None


def find_sources(force: bool = False) -> dict:
    """One real archived message per template. Cached — scanning 823k rows is slow."""
    if CACHE.is_file() and not force:
        return json.loads(CACHE.read_text(encoding="utf-8"))

    found: dict = {}
    for tier in ("tier1", "tier2", "tier3"):
        d = MSGS / tier
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.jsonl")):
            if len(found) == len(_wanted()):
                break
            for line in f.open(encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = (r.get("text") or "").strip()
                if not (60 <= len(t) <= 1500):
                    continue
                for kind, (_, rx) in _wanted().items():
                    kind_needs_company = kind.startswith(("ipo_", "gmp_"))
                    if kind_needs_company and not (_named() and _named().search(t)):
                        continue
                    if kind not in found and rx.search(t):
                        found[kind] = {"kind": kind, "text": t, "tier": tier,
                                       "channel": r.get("channel"),
                                       "channel_id": r.get("channel_id"),
                                       "message_id": r.get("message_id"),
                                       "date": r.get("date")}
                        break
    CACHE.write_text(json.dumps(found, indent=1, ensure_ascii=False), encoding="utf-8")
    return found


def _footer(src: dict) -> str:
    when = str(src.get("date") or "")[:10] or "an earlier date"
    chan = re.sub(r"[^\w\s&.'-]+", "", (src.get("channel") or "a channel").split("|")[0])
    return (f"THIS IS A SAMPLE. It replays a real message posted by {chan.strip()[:40]} "
            f"on {when}, to show you what a live alert looks like. Do not act on it — "
            f"the dates and prices are from {when}.")


async def build(kinds=None, client=None) -> list:
    """Run each source message through the LIVE pipeline and render its email."""
    own = client is None
    client = client or httpx.AsyncClient()
    sources = find_sources()
    try:
        out = []
        for kind, src in sources.items():
            if kinds and kind not in kinds:
                continue
            agent = _wanted()[kind][0]
            ev = {"id": None, "channel_id": BAND - abs(hash(kind)) % 90,
                  "message_id": 300_000 + abs(hash(kind)) % 900,
                  "channel_title": src.get("channel"), "tier": src.get("tier"),
                  "text": src["text"], "posted_at": None, "urls": [],
                  "has_media": False}
            oc = await subagents.run(agent, client, ev, {})
            ctx = {"fields": oc.fields, "headline": oc.headline,
                   "about": (oc.headline or "").split(": ", 1)[-1],
                   "raw": src["text"],
                   "trust": {"title": src.get("channel"), "tier": src.get("tier")}}
            email = _render().render(kind, ctx)
            out.append({"kind": kind, "email": email, "source": src,
                        "agent": oc.agent, "action": oc.action})

        # The two templates with no single source message: a digest of several, and the
        # health report, which is built from live counters rather than from any message.
        if not kinds or "daily_digest" in (kinds or []):
            items = [{"headline": (s["text"].splitlines() or [""])[0][:90]}
                     for s in list(sources.values())[:5]]
            out.append({"kind": "daily_digest",
                        "email": _render().render("daily_digest",
                                               {"items": items, "date": "a past day"}),
                        "source": {"date": "various", "channel": "several channels"},
                        "agent": "-", "action": "digest"})
        if not kinds or "system_health" in (kinds or []):
            import selfreport
            email, _stats, _ok = selfreport.build_report(24)
            out.append({"kind": "system_health", "email": email,
                        "source": {"date": "today", "channel": "live counters"},
                        "agent": "-", "action": "notify"})
        return out
    finally:
        if own:
            await client.aclose()


def send_one(item: dict, notifier=None, respect_limits: bool = True) -> dict:
    """Deliver one sample, marked, deduplicated and rate-limited like anything else."""
    notifier = notifier or default_notifier()
    email = item["email"]
    subject = (PREFIX + email.subject)[:78]
    body = email.text + "\n\n" + _footer(item["source"])
    html = email.html + (
        '<div style="margin-top:14px;padding:10px;border-radius:6px;background:#f3f3f3;'
        'font-size:13px;color:#555">' + _footer(item["source"]) + "</div>")

    if respect_limits:
        v = policy.verdict("sample")
        if v["action"] != "send":
            return {"sent": False, "kind": item["kind"], "why": v["why"]}

    key = f"sample:{item['kind']}:{policy.now_ist():%Y-%m-%d-%H}"
    row = edb.fetch_one(
        """INSERT INTO content.notifications
             (dedupe_key, intent, channel_kind, recipient, subject, body_text,
              body_html, template, status)
           VALUES (%s,'sample',%s,%s,%s,%s,%s,%s,'queued')
           ON CONFLICT (dedupe_key) DO NOTHING RETURNING *""",
        (key, notifier.name, getattr(notifier, "to", "unknown"), subject, body, html,
         email.kind))
    if not row:
        return {"sent": False, "kind": item["kind"], "why": "already sent this hour"}

    receipt = notifier.send(Message(subject, body, html, email.kind, priority=3))
    if receipt.ok:
        adb.mark_sent(row["id"])
    else:
        adb.mark_failed(row["id"], receipt.detail)
    return {"sent": receipt.ok, "kind": item["kind"], "subject": subject,
            "words": email.words, "flesch": email.flesch, "detail": receipt.detail}


async def send_all(notifier=None, kinds=None, respect_limits: bool = False) -> dict:
    """Every template, once. `--all` is a deliberate demonstration, so by default it is
    not throttled — the rate limit exists to protect the reader from a flood of LIVE
    alerts, not from a proof of life they asked for."""
    items = await build(kinds)
    results = [send_one(i, notifier, respect_limits) for i in items]
    return {"templates": len(items), "sent": sum(1 for r in results if r["sent"]),
            "results": results}


async def auto(notifier=None) -> dict:
    """Proof of life: one sample when the market is shut and nothing live has arrived."""
    if market_open():
        return {"sent": False, "why": "market is open — real alerts are expected"}
    age = seconds_since_live_message()
    if age is not None and age < QUIET_FOR_S:
        return {"sent": False,
                "why": f"a live message arrived {int(age // 60)} minutes ago; "
                       f"the chain is demonstrably working"}
    items = await build(["ipo_new"])
    if not items:
        return {"sent": False, "why": "no archived message matched the sample template"}
    out = send_one(items[0], notifier, respect_limits=True)
    out["why"] = (f"market closed and no live message for "
                  f"{'ever' if age is None else str(int(age // 3600)) + 'h'}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="python -m agent.samples")
    ap.add_argument("--all", action="store_true", help="one email per template")
    ap.add_argument("--auto", action="store_true",
                    help="send one only if the market is closed and nothing live arrived")
    ap.add_argument("--kind", action="append", help="limit to these templates")
    ap.add_argument("--dry-run", action="store_true", help="render but send nothing")
    ap.add_argument("--rebuild-sources", action="store_true")
    a = ap.parse_args()

    if a.rebuild_sources:
        print(json.dumps({k: v["date"] for k, v in find_sources(force=True).items()},
                         indent=1))
        raise SystemExit(0)

    n = None
    if a.dry_run:
        from notifier import NullNotifier
        n = NullNotifier()
    if a.auto:
        print(json.dumps(asyncio.run(auto(n)), indent=1, default=str))
    else:
        print(json.dumps(asyncio.run(send_all(n, a.kind)), indent=1, default=str))
