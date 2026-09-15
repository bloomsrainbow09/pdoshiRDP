"""Agent state access. Thin layer over the project's existing Supabase connection.

Reuses `engine.db` rather than opening a second pool: one connection, one source of
truth. The functions here encode the invariants the schema comments describe, so the
rules live in one place instead of being re-implemented at each call site.
"""

import json
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT.parent))

from engine import db as edb  # noqa: E402

SCHEMA = AGENT / "schema.sql"
TERMINAL = {"notify", "digest", "discard", "escalate", "failed"}


def migrate() -> None:
    with edb.cursor() as cur:
        cur.execute(SCHEMA.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ events ----

# Characters Postgres will not accept in a text column, whatever the encoding says.
# NUL is the one that matters: psycopg2 raises ValueError rather than storing it, and
# the exception surfaces at INSERT time — i.e. in the watcher, on a real message, at
# 3 a.m. The adversarial suite found this before a channel did.
_ILLEGAL = str.maketrans({chr(0): None})


def _storable(v):
    """Make a value safe for a Postgres text column without changing its meaning."""
    if isinstance(v, str):
        return v.translate(_ILLEGAL)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False).translate(_ILLEGAL)
    if isinstance(v, (list, tuple)):
        return [x.translate(_ILLEGAL) if isinstance(x, str) else x for x in v]
    return v


def record_event(**f) -> dict | None:
    """Insert a received message. Returns the row, or None if already seen.

    ON CONFLICT DO NOTHING on (channel_id, message_id) is what makes replay after a
    crash free: the gap-replay on startup re-offers everything, and only genuinely new
    messages come back.

    Text is sanitised on the way in. A message carrying a NUL byte made psycopg2 raise
    `ValueError: A string literal cannot contain NUL (0x00) characters` — which in
    production means the watcher losing a message, and possibly the batch around it, to
    a byte nobody typed on purpose.
    """
    cols = [k for k in f if f[k] is not None]
    vals = [_storable(f[k]) for k in cols]
    sql = (f"INSERT INTO content.agent_events ({', '.join(cols)}) "
           f"VALUES ({', '.join(['%s'] * len(cols))}) "
           f"ON CONFLICT (channel_id, message_id) DO NOTHING RETURNING *")
    return edb.fetch_one(sql, tuple(vals))


def decide(event_id: int, decision: str, reason: str, **f) -> None:
    """Give an event its terminal decision.

    `reason` is mandatory and enforced here rather than by convention: a discard
    without a recorded reason is the silent drop this system must never make.
    """
    if decision not in TERMINAL:
        raise ValueError(f"decision must be one of {sorted(TERMINAL)}, got {decision!r}")
    if not (reason or "").strip():
        raise ValueError("a decision without a reason is a silent drop — reason is required")
    sets = ["decision = %s", "decision_reason = %s", "decided_at = now()", "status = 'done'"]
    vals = [decision, reason]
    for k, v in f.items():
        sets.append(f"{k} = %s")
        vals.append(json.dumps(v) if isinstance(v, dict) else v)
    vals.append(event_id)
    edb.execute(f"UPDATE content.agent_events SET {', '.join(sets)} WHERE id = %s", tuple(vals))


# Reserved channel-id bands used by gates, replays and the evaluation harness. Real
# Telegram channels sit around -1.0e12; every synthetic band is between -1.0e9 and -9.0e8.
TEST_BANDS = (-1_000_000_000, -900_000_000)


def pending_events(limit: int = 200, production_only: bool = False) -> list:
    """Received but never decided — the replay set after a crash.

    `production_only` excludes the reserved test bands. The soak needs it: its drain was
    picking up 125 undecided rows from an evaluation run and spending the whole tick on
    them, so the soak stalled and the eval was processed twice. Replay and the eval
    harness deliberately leave it off, because operating on those bands is their job.
    """
    where = "decision IS NULL"
    params: tuple = ()
    if production_only:
        where += " AND (channel_id IS NULL OR channel_id NOT BETWEEN %s AND %s)"
        params = TEST_BANDS
    return edb.fetch_all(
        f"SELECT * FROM content.agent_events WHERE {where} ORDER BY id LIMIT %s",
        params + (limit,))


# -------------------------------------------------------------------- runs ----

def record_run(role: str, res, event_id: int | None = None, cost: float = 0.0) -> None:
    """Log one model call. `res` is an llm.Result."""
    edb.execute(
        """INSERT INTO content.agent_runs
             (event_id, role, provider, model, ok, latency_ms, prompt_tokens,
              completion_tokens, est_cost_usd, attempts, error)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (event_id, role, res.provider, res.model, res.ok, res.latency_ms,
         res.prompt_tokens, res.completion_tokens, cost,
         json.dumps(res.attempts or []), res.error or None))


def spend_since(hours: int = 24) -> dict:
    return edb.fetch_one(
        """SELECT coalesce(sum(est_cost_usd),0) usd, count(*) calls,
                  count(*) FILTER (WHERE NOT ok) failures
           FROM content.agent_runs WHERE created_at > now() - (%s || ' hours')::interval""",
        (hours,))


# ----------------------------------------------------------- notifications ----

def queue_notification(dedupe_key: str, **f) -> dict | None:
    """Queue an email. Returns None if this dedupe_key was already queued or sent.

    That None is the duplicate-suppression mechanism: the same IPO announced by six
    channels produces one row, and a crash between send and mark-sent cannot resend.
    """
    cols = ["dedupe_key"] + [k for k in f if f[k] is not None]
    vals = [dedupe_key] + [f[k] for k in cols[1:]]
    sql = (f"INSERT INTO content.notifications ({', '.join(cols)}) "
           f"VALUES ({', '.join(['%s'] * len(cols))}) "
           f"ON CONFLICT (dedupe_key) DO NOTHING RETURNING *")
    return edb.fetch_one(sql, tuple(vals))


def mark_sent(notif_id: int) -> None:
    edb.execute("UPDATE content.notifications SET status='sent', sent_at=now() WHERE id=%s",
                (notif_id,))


def mark_failed(notif_id: int, err: str) -> None:
    edb.execute("UPDATE content.notifications SET status='failed', error=%s, "
                "attempts=attempts+1 WHERE id=%s", (err[:500], notif_id))


def sent_since(hours: int = 1) -> int:
    return edb.fetch_one(
        "SELECT count(*) n FROM content.notifications WHERE status='sent' "
        "AND sent_at > now() - (%s || ' hours')::interval", (hours,))["n"]


# ------------------------------------------------------------------- state ----

def cursor_for(channel_id: int) -> int:
    r = edb.fetch_one("SELECT last_processed_id FROM content.agent_state WHERE key=%s",
                      (f"channel:{channel_id}",))
    return int(r["last_processed_id"]) if r else 0


def advance_cursor(channel_id: int, message_id: int) -> None:
    """Only ever moves forward — GREATEST guards against an out-of-order replay
    rewinding a channel and re-delivering everything after it."""
    edb.execute(
        """INSERT INTO content.agent_state (key, channel_id, last_processed_id, last_seen_at, updated_at)
           VALUES (%s,%s,%s,now(),now())
           ON CONFLICT (key) DO UPDATE
             SET last_processed_id = GREATEST(content.agent_state.last_processed_id, EXCLUDED.last_processed_id),
                 last_seen_at = now(), updated_at = now()""",
        (f"channel:{channel_id}", channel_id, message_id))


def acquire_lease(account: str, worker_id: str, seconds: int = 300) -> bool:
    """Single-watcher lease. True only if this worker now holds it.

    Two clients on one Telegram session risk the auth key being REVOKED, which needs a
    human with a phone. The 6-hour handoff overlaps by ~5 minutes, so this is the
    routine case, not an edge case.
    """
    r = edb.fetch_one(
        """INSERT INTO content.agent_state (key, worker_id, lease_until, heartbeat_at, updated_at)
           VALUES (%s,%s, now() + (%s || ' seconds')::interval, now(), now())
           ON CONFLICT (key) DO UPDATE
             SET worker_id = EXCLUDED.worker_id,
                 lease_until = EXCLUDED.lease_until,
                 heartbeat_at = now(), updated_at = now()
             WHERE content.agent_state.lease_until IS NULL
                OR content.agent_state.lease_until < now()
                OR content.agent_state.worker_id = EXCLUDED.worker_id
           RETURNING worker_id""",
        (f"watcher:{account}", worker_id, seconds))
    return bool(r and r["worker_id"] == worker_id)


def heartbeat(account: str, worker_id: str, seconds: int = 300) -> None:
    edb.execute(
        """UPDATE content.agent_state
           SET heartbeat_at = now(), lease_until = now() + (%s || ' seconds')::interval,
               updated_at = now()
           WHERE key = %s AND worker_id = %s""",
        (seconds, f"watcher:{account}", worker_id))


def release_lease(account: str, worker_id: str) -> None:
    edb.execute("UPDATE content.agent_state SET lease_until = now() "
                "WHERE key=%s AND worker_id=%s", (f"watcher:{account}", worker_id))


# ------------------------------------------------------------------ errors ----

def dead_letter(stage: str, error_class: str, message: str,
                event_id: int | None = None, context: dict | None = None) -> dict:
    """Record a failure. Never fails to record one.

    `event_id` references `agent_events`, and a failure noticed after its event row has
    been cleaned up — the evaluation and gate bands are deleted routinely — violated the
    foreign key and raised. The one place whose entire job is making sure nothing is
    lost was itself losing the report. The id is kept in `context` so nothing about the
    failure is lost either.
    """
    sql = ("""INSERT INTO content.agent_errors (event_id, stage, error_class, message, context)
              VALUES (%s,%s,%s,%s,%s) RETURNING *""")
    ctx = dict(context or {})
    try:
        return edb.fetch_one(sql, (event_id, stage, error_class, message[:2000],
                                   json.dumps(ctx)))
    except Exception:
        ctx["orphaned_event_id"] = event_id
        return edb.fetch_one(sql, (None, stage, error_class, message[:2000],
                                   json.dumps(ctx)))


def open_errors(limit: int = 50) -> list:
    return edb.fetch_all(
        "SELECT * FROM content.agent_errors WHERE NOT resolved "
        "ORDER BY created_at DESC LIMIT %s", (limit,))
