"""Survival: circuit breakers, heartbeat, dead-letter escalation, daily self-report.

This runs for weeks with nobody watching, on free-tier providers that go down without
notice. Everything here exists because it was observed during the build, not imagined:

  * Kilo returned HTTP 200 with no `choices` and the caller saw `KeyError: 'choices'`
  * Z.ai returned 429 "rate limit reached" for an hour
  * `gemma-4-31b-it` and `mistral-nemotron` both went to read-timeout mid-session
  * a 500-message job stalled for 45 minutes at 0.14s of CPU, retrying into an outage

The pattern is `error_router → bug_report → escalate`, deliberately WITHOUT the
`self_healing_code` corpus's fourth step. It diagnoses and reports; it does not patch
live code. An agent editing its own running source at 3 a.m. with nobody watching is a
way to turn one bad morning into an unrecoverable one.

**Circuit breakers are advisory, never fatal.** A tripped breaker moves a model to the
back of its chain; it can never leave a chain empty. Losing a message because every
provider looked unhealthy is a worse outcome than trying a model that is probably down.
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

AGENT = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT))
sys.path.insert(0, str(AGENT.parent))

import db as adb                    # noqa: E402
from engine import db as edb        # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))

# Reserved channel-id bands used by gates, replays and the evaluation harness. Real
# Telegram channels are around -1.0e12; every synthetic band sits between -1.0e9 and
# -9.0e8. The daily report must exclude them — a health email that counts 267 half-
# processed EVAL rows as "messages read but never decided" is telling the user their
# system is broken when it is only being tested.
TEST_BAND_LO, TEST_BAND_HI = -1_000_000_000, -900_000_000
NOT_TEST = "(channel_id IS NULL OR channel_id NOT BETWEEN -1000000000 AND -900000000)"

# A model is tripped after this many consecutive failures, and stays tripped this long.
TRIP_AFTER = 3
TRIP_FOR_S = 900                    # 15 minutes — long enough for a rate window to roll
HEARTBEAT_S = 60
STALE_AFTER_S = 900                 # 15 minutes without a beat means the runner died
DEAD_LETTER_ATTEMPTS = 3


# ─────────────────────────────────────────────────────────── circuit breaker ────

class Breaker:
    """Per-model failure tracking, shared across a process.

    State lives in memory rather than the database on purpose: a breaker is about THIS
    runner's view of a provider right now, and the 6-hour handoff should start with a
    clean opinion rather than inheriting a stale one from a runner that is already gone.
    """

    def __init__(self, trip_after: int = TRIP_AFTER, trip_for: float = TRIP_FOR_S):
        self.trip_after, self.trip_for = trip_after, trip_for
        self._fails: dict = {}
        self._until: dict = {}
        self._episodes: dict = {}

    def record(self, model: str, ok: bool, error: str = "") -> None:
        if ok:
            self._fails.pop(model, None)
            self._until.pop(model, None)
            return
        n = self._fails.get(model, 0) + 1
        self._fails[model] = n
        if n >= self.trip_after:
            first_trip = model not in self._until
            self._until[model] = time.time() + self.trip_for
            # Report the EPISODE, not every re-trip. A model that is down trips again
            # the moment its bench expires, and the first hour of the soak produced 48
            # dead-letters for one outage — which buries a real error in noise. The
            # count is carried so the report still says how bad it got.
            self._episodes[model] = self._episodes.get(model, 0) + (1 if first_trip else 0)
            if first_trip:
                try:
                    adb.dead_letter("circuit_breaker", "model_tripped",
                                    f"{model} failed {n} times in a row; benched for "
                                    f"{int(self.trip_for)}s. Last error: {error[:200]}",
                                    context={"model": model, "failures": n,
                                             "episode": self._episodes[model]})
                except Exception:
                    pass

    def is_open(self, model: str) -> bool:
        until = self._until.get(model)
        if not until:
            return False
        if time.time() >= until:
            self._until.pop(model, None)
            self._fails.pop(model, None)
            return False
        return True

    def order(self, chain: list) -> list:
        """Healthy models first, benched ones last — never removed.

        A chain that has been emptied by breakers cannot deliver anything, and the whole
        point of this system is that a message is never lost. A benched model at the
        back costs one failed call; an empty chain costs the message.
        """
        healthy = [c for c in chain if not self.is_open(c[1])]
        benched = [c for c in chain if self.is_open(c[1])]
        return healthy + benched

    def status(self) -> dict:
        return {"tripped": {m: int(t - time.time()) for m, t in self._until.items()
                            if t > time.time()},
                "failing": dict(self._fails),
                "episodes": dict(self._episodes)}


BREAKER = Breaker()


# ─────────────────────────────────────────────────────────────── heartbeat ────

def beat(worker: str, note: str = "") -> None:
    """Record that this runner is alive. Cheap, and the only thing that distinguishes
    'quiet market' from 'the process died four hours ago'."""
    edb.execute(
        """INSERT INTO content.agent_state (key, worker_id, heartbeat_at, updated_at, meta)
           VALUES (%s, %s, now(), now(), %s::jsonb)
           ON CONFLICT (key) DO UPDATE
             SET worker_id = EXCLUDED.worker_id, heartbeat_at = now(),
                 updated_at = now(), meta = EXCLUDED.meta""",
        (f"heartbeat:{worker}", worker, json.dumps({"note": note[:200]})))


def last_beat(worker: str) -> dict:
    r = edb.fetch_one(
        "SELECT worker_id, heartbeat_at, meta, "
        "extract(epoch from (now() - heartbeat_at)) AS age_s "
        "FROM content.agent_state WHERE key = %s", (f"heartbeat:{worker}",))
    if not r:
        return {"alive": False, "age_s": None, "why": "no heartbeat ever recorded"}
    age = float(r["age_s"] or 0)
    return {"alive": age <= STALE_AFTER_S, "age_s": int(age),
            "worker_id": r["worker_id"], "note": (r["meta"] or {}).get("note"),
            "why": ("beating" if age <= STALE_AFTER_S
                    else f"stale — last beat {int(age)}s ago, threshold {STALE_AFTER_S}s")}


def stale_workers() -> list:
    """Runners that stopped beating. The next runner uses this to know it must replay."""
    return edb.fetch_all(
        """SELECT key, worker_id, heartbeat_at,
                  extract(epoch from (now() - heartbeat_at))::int age_s
             FROM content.agent_state
            WHERE key LIKE 'heartbeat:%%'
              AND heartbeat_at < now() - (%s || ' seconds')::interval
            ORDER BY heartbeat_at""", (STALE_AFTER_S,))


# ───────────────────────────────────────────────────────────── dead letter ────

def fail(stage: str, error_class: str, message: str, event_id=None,
         context: dict | None = None) -> dict:
    """Record a failure and count it. Three strikes and it is escalated by email."""
    row = adb.dead_letter(stage, error_class, message, event_id, context)
    n = edb.fetch_one(
        """SELECT count(*) n FROM content.agent_errors
            WHERE stage = %s AND error_class = %s AND NOT resolved
              AND created_at > now() - interval '24 hours'""",
        (stage, error_class))["n"]
    edb.execute("UPDATE content.agent_errors SET attempts = %s WHERE id = %s",
                (n, row["id"]))
    return {"id": row["id"], "attempts": n,
            "escalate": n >= DEAD_LETTER_ATTEMPTS}


def pending_escalations() -> list:
    """Failures that have hit the threshold and have not yet been emailed.

    Grouped by (stage, error_class): one email about a provider that failed forty times
    is useful, forty emails is the same outage plus a second problem.
    """
    return edb.fetch_all(
        """SELECT stage, error_class, count(*) n, max(created_at) latest,
                  min(created_at) first_seen,
                  (array_agg(message ORDER BY created_at DESC))[1] AS last_message,
                  array_agg(id) AS ids
             FROM content.agent_errors
            WHERE NOT resolved AND NOT alerted
              AND created_at > now() - interval '24 hours'
            GROUP BY 1, 2
           HAVING count(*) >= %s
            ORDER BY 3 DESC""", (DEAD_LETTER_ATTEMPTS,))


def mark_alerted(ids: list) -> None:
    edb.execute("UPDATE content.agent_errors SET alerted = true WHERE id = ANY(%s)",
                (list(ids),))


def resolve(stage: str, error_class: str | None = None) -> int:
    rows = edb.fetch_all(
        "UPDATE content.agent_errors SET resolved = true "
        "WHERE stage = %s AND (%s::text IS NULL OR error_class = %s) AND NOT resolved "
        "RETURNING id", (stage, error_class, error_class))
    return len(rows)


# ──────────────────────────────────────────────────────── daily self-report ────

def daily_stats(hours: int = 24) -> dict:
    """Everything the user needs to know the system is alive, in one query each."""
    ev = edb.fetch_one(
        """SELECT count(*) seen,
                  count(*) FILTER (WHERE decision = 'notify')   notified,
                  count(*) FILTER (WHERE decision = 'digest')   digested,
                  count(*) FILTER (WHERE decision = 'discard')  discarded,
                  count(*) FILTER (WHERE decision = 'escalate') escalated,
                  count(*) FILTER (WHERE decision = 'failed')   failed,
                  count(*) FILTER (WHERE decision IS NULL)      undecided,
                  max(received_at) AS newest
             FROM content.agent_events
            WHERE received_at > now() - (%s || ' hours')::interval
              AND """ + NOT_TEST, (hours,))
    notif = edb.fetch_one(
        """SELECT count(*) FILTER (WHERE status = 'sent')       sent,
                  count(*) FILTER (WHERE status = 'queued')     queued,
                  count(*) FILTER (WHERE status = 'held')       held,
                  count(*) FILTER (WHERE status = 'suppressed') suppressed,
                  count(*) FILTER (WHERE status = 'failed')     failed
             FROM content.notifications
            WHERE created_at > now() - (%s || ' hours')::interval
              AND dedupe_key NOT LIKE 'p9test:%%'""", (hours,))
    runs = edb.fetch_one(
        """SELECT count(*) calls, count(*) FILTER (WHERE NOT ok) failures,
                  coalesce(sum(est_cost_usd), 0) usd,
                  coalesce(avg(latency_ms), 0) avg_ms
             FROM content.agent_runs
            WHERE created_at > now() - (%s || ' hours')::interval""", (hours,))
    health = edb.fetch_all(
        """SELECT model, count(*) n, count(*) FILTER (WHERE NOT ok) bad,
                  round(avg(latency_ms)) ms
             FROM content.agent_runs
            WHERE created_at > now() - (%s || ' hours')::interval
            GROUP BY 1 ORDER BY 2 DESC LIMIT 8""", (hours,))
    errors = edb.fetch_one(
        "SELECT count(*) n FROM content.agent_errors WHERE NOT resolved "
        "AND created_at > now() - (%s || ' hours')::interval", (hours,))

    calls = runs["calls"] or 0
    return {
        "window_hours": hours,
        "seen": ev["seen"], "notified": ev["notified"], "digested": ev["digested"],
        "discarded": ev["discarded"], "escalated": ev["escalated"],
        "failed": ev["failed"], "undecided": ev["undecided"],
        "newest_message": ev["newest"].isoformat() if ev["newest"] else None,
        "emails_sent": notif["sent"], "emails_held": notif["held"],
        "emails_suppressed": notif["suppressed"], "emails_failed": notif["failed"],
        "model_calls": calls, "model_failures": runs["failures"],
        "cost_usd": round(float(runs["usd"]), 4),
        "avg_latency_ms": int(float(runs["avg_ms"] or 0)),
        "open_errors": errors["n"],
        "model_health": [
            {"model": h["model"].split("/")[-1][:28], "calls": h["n"],
             "failures": h["bad"], "ms": int(h["ms"] or 0),
             "ok_pct": round(100 * (h["n"] - h["bad"]) / max(1, h["n"]), 1)}
            for h in health],
        "breaker": BREAKER.status(),
    }


def health_verdict(s: dict) -> tuple:
    """Is the system actually well? Returns (ok, what_to_do).

    "No errors" is not the same as "working". A day with zero messages seen means the
    watcher is not running, and that looks identical to a quiet market unless it is
    checked for explicitly.
    """
    problems = []
    if s["seen"] == 0:
        problems.append("No messages were read from your channels at all. The watcher "
                        "is probably not running.")
    if s["undecided"] > 20:
        problems.append(f"{s['undecided']} messages were read but never decided.")
    if s["failed"]:
        problems.append(f"{s['failed']} messages hit an error in the pipeline.")
    if s["emails_failed"]:
        problems.append(f"{s['emails_failed']} emails could not be sent.")
    if s["open_errors"] >= DEAD_LETTER_ATTEMPTS:
        problems.append(f"{s['open_errors']} unresolved errors are recorded.")
    if s["model_calls"] and s["model_failures"] / s["model_calls"] > 0.25:
        problems.append("More than a quarter of the model calls failed.")
    return (not problems), problems


if __name__ == "__main__":
    s = daily_stats()
    ok, problems = health_verdict(s)
    print(json.dumps({"ok": ok, "problems": problems, **s}, indent=1, default=str))
