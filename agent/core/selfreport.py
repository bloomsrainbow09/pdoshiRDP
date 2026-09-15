"""The daily self-report, and the dead-letter escalation that cannot wait for it.

Two emails, for two different jobs:

**The daily report** exists because silence is ambiguous. A day with no alerts looks
exactly like a day when the watcher died four hours in, and the reader cannot tell the
difference without being told. So one message a day says what happened, in the same
plain English as every other email — `health_verdict()` decides whether it is a
reassurance or a warning, and the warning says what to do rather than what broke.

**The escalation** goes out as soon as something has failed three times. Grouped by
(stage, error class): one email about a provider that failed forty times is useful,
forty emails is the same outage plus a second problem.

Both go through the same `Notifier` as everything else, so they are rate-limited,
deduplicated and transactional like any other message, and switching to WhatsApp later
changes nothing here.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

AGENT = Path(__file__).resolve().parent
for p in (AGENT, AGENT.parent, AGENT / "templates", AGENT / "delivery"):
    sys.path.insert(0, str(p))

import db as adb                    # noqa: E402
from core.delivery import policy  # noqa: E402

from core import resilience  # noqa: E402  explicit: a vertical may have one too
from engine import db as edb        # noqa: E402
from notifier import Message, default as default_notifier  # noqa: E402

def _render():
    """The ACTIVE vertical's renderer. There is no top-level `render` module: R5 moved
    the templates into the vertical and R7 removed the compatibility shim."""
    from core import active
    return active.module("render")



def build_report(hours: int = 24) -> tuple:
    """(Email, stats, ok). Uses the same system_health template as everything else."""
    s = resilience.daily_stats(hours)
    ok, problems = resilience.health_verdict(s)
    newest = s["newest_message"]
    if newest:
        try:
            newest = datetime.fromisoformat(newest).astimezone(policy.IST).strftime("%H:%M")
        except (ValueError, TypeError):
            newest = str(newest)[:16]
    email = _render().render("system_health", {
        "ok": ok, "events": s["seen"], "sent": s["emails_sent"],
        "errors": s["open_errors"], "last_message": newest,
        "what_to_do": problems[0] if problems else None})
    return email, s, ok


def send_daily(hours: int = 24, notifier=None, force: bool = False) -> dict:
    """One report a day. The dedupe key is the date, so a runner that restarts four
    times between handoffs still sends exactly one."""
    notifier = notifier or default_notifier()
    email, stats, ok = build_report(hours)
    day = policy.now_ist().strftime("%Y-%m-%d")
    key = f"selfreport:{day}" + (f":{policy.now_ist():%H%M%S}" if force else "")

    row = edb.fetch_one(
        """INSERT INTO content.notifications
             (dedupe_key, intent, channel_kind, recipient, subject, body_text,
              body_html, template, status)
           VALUES (%s,'system_health',%s,%s,%s,%s,%s,%s,'queued')
           ON CONFLICT (dedupe_key) DO NOTHING RETURNING *""",
        (key, notifier.name, getattr(notifier, "to", "unknown"), email.subject,
         email.text, email.html, email.kind))
    if not row:
        return {"sent": False, "why": "already reported today", "key": key, "ok": ok}

    receipt = notifier.send(Message(email.subject, email.text, email.html, email.kind,
                                    priority=3))
    if receipt.ok:
        adb.mark_sent(row["id"])
    else:
        adb.mark_failed(row["id"], receipt.detail)
    return {"sent": receipt.ok, "healthy": ok, "detail": receipt.detail,
            "subject": email.subject, "stats": stats}


def escalate_failures(notifier=None) -> dict:
    """Email anything that has failed three times and not yet been reported.

    A dead letter that nobody is told about is just a slower way of dropping the
    message — which is the failure mode the whole system is built to avoid.
    """
    notifier = notifier or default_notifier()
    groups = resilience.pending_escalations()
    if not groups:
        return {"escalated": 0, "why": "nothing has failed three times"}

    sent = 0
    for g in groups:
        # Six-hourly, not hourly. One provider outage produced four identical
        # "Alerts problem" emails during the soak, which is the same noise the circuit
        # breaker was making before D-19 - and four copies of a warning is how a reader
        # learns to ignore the warning.
        bucket = policy.now_ist().hour // 6
        key = (f"deadletter:{g['stage']}:{g['error_class']}:"
               f"{policy.now_ist():%Y-%m-%d}-{bucket}")
        email = _render().render("system_health", {
            "ok": False,
            "events": g["n"], "sent": 0, "errors": g["n"],
            "last_message": g["latest"].astimezone(policy.IST).strftime("%H:%M"),
            "what_to_do": f"{g['stage']} has failed {g['n']} times since "
                          f"{g['first_seen'].astimezone(policy.IST):%H:%M}. "
                          f"Latest: {str(g['last_message'])[:110]}"})
        row = edb.fetch_one(
            """INSERT INTO content.notifications
                 (dedupe_key, intent, channel_kind, recipient, subject, body_text,
                  body_html, template, status)
               VALUES (%s,'regulatory',%s,%s,%s,%s,%s,%s,'queued')
               ON CONFLICT (dedupe_key) DO NOTHING RETURNING *""",
            (key, notifier.name, getattr(notifier, "to", "unknown"),
             f"Alerts problem: {g['stage']} failing"[:60], email.text, email.html,
             email.kind))
        if not row:
            continue
        receipt = notifier.send(Message(f"Alerts problem: {g['stage']} failing"[:60],
                                        email.text, email.html, email.kind, priority=1))
        if receipt.ok:
            adb.mark_sent(row["id"])
            resilience.mark_alerted(g["ids"])
            sent += 1
        else:
            adb.mark_failed(row["id"], receipt.detail)
    return {"escalated": sent, "groups": len(groups)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--escalate", action="store_true",
                    help="send dead-letter escalations instead of the daily report")
    a = ap.parse_args()
    n = None
    if a.dry_run:
        from notifier import NullNotifier
        n = NullNotifier()
    out = escalate_failures(n) if a.escalate else send_daily(a.hours, n)
    print(json.dumps(out, indent=1, default=str))
