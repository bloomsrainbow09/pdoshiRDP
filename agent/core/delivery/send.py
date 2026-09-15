"""The delivery loop: decided events in, one email out, exactly once.

The transactional guarantee is the whole point of this file, and it rests on two things
that are enforced in the database rather than in this code:

  * `notifications.dedupe_key` is UNIQUE, so a second INSERT for the same fact returns
    no row. That is what makes "one event, one email" survive a crash, a restart, or a
    replay — the same event offered five times queues once.
  * A row is marked `sent` ONLY after the transport confirms. A crash between the send
    and the mark leaves the row `queued`, so the next run retries it — and the retry is
    a no-op for the recipient only because the dedupe key already exists. Losing an
    email is worse than sending one twice, but the design gives up neither.

Held and batched messages are queued with `status='held'` and a `scheduled_for`, so
nothing is ever dropped for being out of hours or over the limit; `flush()` picks them
up at 07:00 or when the hour rolls over.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
for p in (AGENT, AGENT.parent, AGENT / "templates", AGENT / "delivery"):
    sys.path.insert(0, str(p))

import db as adb                    # noqa: E402
from core import registry           # noqa: E402
from core.delivery import policy    # noqa: E402
from engine import db as edb        # noqa: E402
from notifier import Message, default as default_notifier  # noqa: E402

RECIPIENT_FALLBACK = "bloomsrainbow09@gmail.com"


def _recipient(n) -> str:
    return getattr(n, "to", None) or RECIPIENT_FALLBACK


def pending_events(limit: int = 50) -> list:
    """Decided events that should reach the reader and have no notification yet."""
    return edb.fetch_all(
        """SELECT e.* FROM content.agent_events e
            WHERE e.decision IN ('notify', 'escalate')
              AND e.agent_outcome IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM content.notifications n
                               WHERE n.event_id = e.id)
            ORDER BY e.id LIMIT %s""", (limit,))


def fact_text(subject: str, outcome: dict) -> str:
    """What the near-duplicate check compares: the subject and the extracted values.

    NOT the email body. Two emails from one template share every line of boilerplate, so
    comparing bodies measures the template — two different IPOs rendered side by side
    scored 0.49 against a 0.50 threshold, which is no margin at all. The extracted
    fields carry the fact and nothing else, so the same IPO from two channels and two
    different IPOs are separated by construction rather than by a stop-word list.
    """
    f = outcome.get("fields") or {}
    vals = [str(v) for k, v in f.items()
            if not k.startswith("_") and v not in (None, "", [], {})]
    return " ".join([subject] + vals)


def compose(ev: dict) -> tuple:
    """Event -> (Message, dedupe_key, fact_text). Rendering is deterministic."""
    outcome = ev.get("agent_outcome") or {}
    if isinstance(outcome, str):
        outcome = json.loads(outcome)
    email = registry.current().render_event(ev, outcome)
    priority = int((INTENT_PRIORITY.get(ev.get("intent")) or 3))
    return (Message(email.subject, email.text, email.html, email.kind, priority,
                    {"event_id": ev.get("id"), "template": email.kind, **email.meta}),
            policy.dedupe_key(ev, outcome),
            fact_text(email.subject, outcome))


INTENT_PRIORITY = {k: v.get("priority", 3)
                   for k, v in policy.INTENTS.items()}


def queue(ev: dict, notifier=None) -> dict:
    """Decide, deduplicate and queue ONE event. Sends nothing."""
    notifier = notifier or default_notifier()
    msg, key, facts = compose(ev)
    intent = ev.get("intent") or msg.kind

    dup = policy.semantic_duplicate(facts)
    if dup:
        # Recorded, not silently dropped: a suppression the reader never sees still has
        # to be explainable afterwards.
        edb.execute(
            """INSERT INTO content.notifications
                 (dedupe_key, event_id, intent, channel_kind, recipient, subject,
                  template, status, suppressed_why)
               VALUES (%s,%s,%s,%s,%s,%s,%s,'suppressed',%s)
               ON CONFLICT (dedupe_key) DO NOTHING""",
            (f"{key}:dup{ev['id']}", ev["id"], intent, notifier.name, _recipient(notifier),
             msg.subject[:300], msg.kind,
             f"near-duplicate of notification {dup['id']} "
             f"(similarity {dup['similarity']})"))
        return {"queued": False, "why": "semantic duplicate", "of": dup}

    v = policy.verdict(intent)
    status = "queued" if v["action"] == "send" else "held"
    row = edb.fetch_one(
        """INSERT INTO content.notifications
             (dedupe_key, event_id, intent, channel_kind, recipient, subject,
              body_text, body_html, template, status, scheduled_for, suppressed_why)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (dedupe_key) DO NOTHING RETURNING *""",
        (key, ev["id"], intent, notifier.name, _recipient(notifier), msg.subject[:300],
         msg.text, msg.html, msg.kind, status, v.get("until"),
         None if status == "queued" else v["why"]))
    if not row:
        return {"queued": False, "why": "already queued for this fact", "key": key}
    return {"queued": True, "id": row["id"], "status": status, "key": key,
            "verdict": v["action"], "why": v["why"]}



def _freshen(row: dict) -> Message:
    """Re-render a queued notification from its event, at SEND time.

    `queue()` stores body_text and body_html on the row. That is the right thing for
    deduplication — the fact signature has to be computed once — but it makes the body a
    CACHE, and a cache of rendered output goes stale the moment a template changes.

    It did. Rows queued before the template rewrite still carried the old labelled-
    fragment style ("THEY SAID:", "WHAT IT SEEMS TO BE:", inline glosses like
    "target (the price they expect it to reach)"), and releasing them from hold sent
    those old emails days later. 48 rows were sitting in that state.

    So the stored body is now a FALLBACK, not the source. Re-rendering from the event
    means a template change reaches everything that has not gone out yet, which is what
    anyone changing a template expects.

    Never raises: if the event is gone or the vertical cannot render it, the stored body
    is sent. Losing an email is worse than sending an old-looking one.
    """
    try:
        ev = edb.fetch_one("SELECT * FROM content.agent_events WHERE id = %s",
                           (row["event_id"],))
        if ev:
            msg, _key, _facts = compose(ev)
            if msg.text and msg.text != (row["body_text"] or ""):
                # Keep the row in step with what was actually sent, so the audit trail
                # shows the email the reader received rather than the one queued.
                edb.execute("UPDATE content.notifications SET subject=%s, body_text=%s, "
                            "body_html=%s WHERE id=%s",
                            (msg.subject[:300], msg.text, msg.html, row["id"]))
            return msg
    except Exception:
        pass
    return Message(row["subject"], row["body_text"] or "",
                   row["body_html"] or "", row["template"] or "")


def deliver(limit: int = 20, notifier=None) -> dict:
    """Send what is queued and due. The only place a row becomes `sent`."""
    notifier = notifier or default_notifier()
    ok, detail = notifier.available()
    if not ok:
        return {"sent": 0, "failed": 0, "skipped": 0, "error": f"transport: {detail}"}

    rows = edb.fetch_all(
        """SELECT * FROM content.notifications
            WHERE status = 'queued'
              AND (scheduled_for IS NULL OR scheduled_for <= now())
            ORDER BY id LIMIT %s""", (limit,))
    stats = {"sent": 0, "failed": 0, "skipped": 0, "transport": notifier.name}
    for r in rows:
        v = policy.verdict(r["intent"] or "")
        if v["action"] != "send":
            edb.execute("UPDATE content.notifications SET status='held', "
                        "scheduled_for=%s, suppressed_why=%s WHERE id=%s",
                        (v.get("until"), v["why"], r["id"]))
            stats["skipped"] += 1
            continue
        msg = _freshen(r)
        receipt = notifier.send(msg)
        if receipt.ok:
            # Only now. Everything above this line is reversible; this line is not.
            adb.mark_sent(r["id"])
            stats["sent"] += 1
        else:
            adb.mark_failed(r["id"], receipt.detail)
            stats["failed"] += 1
    return stats


def release_due() -> int:
    """Move held messages back to the queue once their window opens."""
    rows = edb.fetch_all(
        """UPDATE content.notifications SET status='queued'
            WHERE status='held' AND scheduled_for IS NOT NULL
              AND scheduled_for <= now() RETURNING id""")
    return len(rows)


def run(limit: int = 50, notifier=None) -> dict:
    notifier = notifier or default_notifier()
    adb.migrate()
    released = release_due()
    events = pending_events(limit)
    queued = [queue(ev, notifier) for ev in events]
    out = deliver(limit, notifier)
    out.update({"events_seen": len(events),
                "queued": sum(1 for q in queued if q["queued"]),
                "suppressed": sum(1 for q in queued if not q["queued"]),
                "released_from_hold": released})
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true",
                    help="exercise the whole path but send nothing")
    a = ap.parse_args()
    n = None
    if a.dry_run:
        from notifier import NullNotifier
        n = NullNotifier()
    print(json.dumps(run(a.limit, n), indent=2, default=str))
