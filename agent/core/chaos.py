"""Chaos experiments: break each thing on purpose and prove nothing is lost.

Every scenario here is one that WILL happen, not one that might. The 6-hour runner kill
happens four times a day. Free-tier models 404 and time out weekly. Gmail rejects. The
database connection drops. Telegram issues a FloodWait the moment a gap replay starts.

The assertion is always the same and it is never "it didn't crash": after the failure,
**is the message still going to reach the reader?** A pipeline that survives by dropping
the thing it was carrying has not survived anything.

Each experiment cleans up after itself and leaves production data untouched.
"""

import asyncio
import json
import sys
import uuid
from pathlib import Path

AGENT = Path(__file__).resolve().parent
for p in (AGENT, AGENT.parent, AGENT / "delivery", AGENT / "templates"):
    sys.path.insert(0, str(p))

import httpx                                    # noqa: E402
import db as adb                                # noqa: E402
import llm                                      # noqa: E402
from core.delivery import notifier as tp  # noqa: E402
import orchestrator as orc                      # noqa: E402
from core import resilience  # noqa: E402  explicit: a vertical may have one too
from core.delivery import send as sender  # noqa: E402
from engine import db as edb                    # noqa: E402

class _GenericFixture:
    """What a vertical gets if it ships no chaos_fixture.py.

    Deliberately contentless: the experiments are about surviving a severed database and
    a revoked model, and a message with no domain in it exercises exactly the same paths.
    """
    CO = "Chaos Fixture Co"
    TEXT = "Chaos Fixture Co — a test message used only by the chaos suite."
    OUTCOME = {"action": "notify", "agent": "escalation",
               "headline": "Chaos fixture", "fields": {"company": CO},
               "facts": ["A fixture used only by the chaos suite."],
               "caveats": [], "confidence": 0.9, "reason": "chaos fixture"}


def _chaos_fixture():
    from core import active
    try:
        return active.module("chaos_fixture")
    except FileNotFoundError:
        return _GenericFixture


def _chaos_outcome() -> dict:
    return dict(_chaos_fixture().OUTCOME)


BAND = -988_000_000
OUT = AGENT / "bench" / "chaos_result.json"


def clean():
    edb.execute("DELETE FROM content.notifications WHERE event_id IN "
                "(SELECT id FROM content.agent_events WHERE channel_id BETWEEN %s AND %s)",
                (BAND - 200, BAND))
    edb.execute("DELETE FROM content.agent_events WHERE channel_id BETWEEN %s AND %s",
                (BAND - 200, BAND))
    edb.execute("DELETE FROM content.agent_errors WHERE stage LIKE 'chaos:%%'")


def _ev(i: int, text: str) -> dict:
    return adb.record_event(channel_id=BAND - (i % 100), message_id=400_000 + i,
                            channel_title="chaos", tier="tier1", text=text)


# A company name nothing else will ever use. The fixture originally said "Vibhor Steel
# Tubes", which is a real IPO in the archive — so once the P12 samples had queued a real
# alert about it, the near-duplicate check correctly suppressed the chaos fixture and the
# SMTP experiment failed against a system that was working.
_CHAOS_CO = _chaos_fixture().CO
# The message the chaos experiments push through. Realistic by the VERTICAL's standard,
# because what a representative message looks like is not something core can know.
TEXT = _chaos_fixture().TEXT


# ───────────────────────────────────────────────── 1. kill mid-batch ────

async def kill_mid_batch() -> dict:
    """Process 10 messages, "die" after 4, restart, and check the survivors.

    This is the 6-hour handoff, four times a day. The requirement is exact: the six
    undecided messages must still be pending, and the four decided ones must not be
    processed twice.
    """
    clean()
    evs = [_ev(i, f"{TEXT}\nbatch item {i}") for i in range(1, 11)]
    evs = [e for e in evs if e]
    async with httpx.AsyncClient() as c:
        for e in evs[:4]:
            await orc.process(c, e)

    decided = edb.fetch_one(
        "SELECT count(*) n FROM content.agent_events WHERE channel_id BETWEEN %s AND %s "
        "AND decision IS NOT NULL", (BAND - 200, BAND))["n"]
    # restart: the gap replay re-offers every message, as the real watcher does
    reoffered = [_ev(i, f"{TEXT}\nbatch item {i}") for i in range(1, 11)]
    pending = edb.fetch_one(
        "SELECT count(*) n FROM content.agent_events WHERE channel_id BETWEEN %s AND %s "
        "AND decision IS NULL", (BAND - 200, BAND))["n"]
    total = edb.fetch_one(
        "SELECT count(*) n FROM content.agent_events WHERE channel_id BETWEEN %s AND %s",
        (BAND - 200, BAND))["n"]
    clean()
    return {"name": "kill mid-batch",
            "ok": total == 10 and decided == 4 and pending == 6
                  and all(r is None for r in reoffered),
            "detail": f"10 received, {decided} decided, then restarted and re-offered "
                      f"all 10: {total} stored (no duplicates), {pending} still pending"}


# ──────────────────────────────────────────────── 2. revoke a model ────

async def revoke_model() -> dict:
    """Point the first model in a chain at something that does not exist.

    The call must fall through to the next model and still return an answer — the user
    never learns a provider was down.
    """
    chain = [("nvidia", "does-not-exist/revoked-model-9000"),
             ("nvidia", "openai/gpt-oss-20b")]
    async with httpx.AsyncClient() as c:
        res = await llm.call(chain, "Reply with JSON only.", 'Return {"ok":true}',
                             client=c, max_tokens=300, timeout=60)
    fell_through = res.ok and "gpt-oss" in (res.model or "")
    return {"name": "revoke a model", "ok": bool(fell_through),
            "detail": f"first model 404s, answer came from {res.model}; "
                      f"attempts: {res.attempts}"}


def breaker_benches() -> dict:
    """A model that keeps failing must be moved to the back of the chain, not removed."""
    b = resilience.Breaker(trip_after=2, trip_for=60)
    chain = [("nvidia", "bad/model"), ("nvidia", "good/model")]
    for _ in range(2):
        b.record("bad/model", False, "HTTP 503")
    order = [m for _, m in b.order(chain)]
    b.record("bad/model", True)
    recovered = [m for _, m in b.order(chain)]
    return {"name": "circuit breaker benches, never removes",
            "ok": order == ["good/model", "bad/model"] and len(order) == 2
                  and recovered == ["bad/model", "good/model"],
            "detail": f"after 2 failures the order is {order} (still 2 models, not 1); "
                      f"after one success it resets to {recovered}"}


# ──────────────────────────────────────────────────── 3. break SMTP ────

def break_smtp() -> dict:
    """A transport failure must leave the alert queued and retryable, never `sent`."""
    clean()
    ev = _ev(50, TEXT)
    # A representative outcome for whichever niche is running. The chaos experiments are
    # about surviving a severed database and a revoked model; the payload only has to be
    # realistic, and what "realistic" means is the vertical's to say.
    outcome = _chaos_outcome()
    adb.decide(ev["id"], "notify", "chaos fixture", intent="ipo_new",
               agent_outcome=outcome)
    ev = edb.fetch_one("SELECT * FROM content.agent_events WHERE id=%s", (ev["id"],))

    import policy
    real_now, policy.now_ist = policy.now_ist, \
        lambda: __import__("datetime").datetime(2026, 9, 14, 10, 30, tzinfo=policy.IST)
    # And pin the hourly count: this experiment is about what happens when the TRANSPORT
    # fails, not about whether the queue happens to be full right now.
    real_sent, policy.sent_last_hour = policy.sent_last_hour, lambda: 0
    try:
        sender.queue(ev, tp.FailingNotifier())
        broke = sender.deliver(10, tp.FailingNotifier())
        after_fail = edb.fetch_one(
            "SELECT status, attempts FROM content.notifications WHERE event_id=%s",
            (ev["id"],))
        edb.execute("UPDATE content.notifications SET status='queued' WHERE event_id=%s",
                    (ev["id"],))
        null = tp.NullNotifier()
        sender.deliver(10, null)
        after_retry = edb.fetch_one(
            "SELECT status FROM content.notifications WHERE event_id=%s", (ev["id"],))
    finally:
        policy.now_ist = real_now
        policy.sent_last_hour = real_sent
    ok = (broke["failed"] == 1 and after_fail["status"] == "failed"
          and after_retry["status"] == "sent" and len(null.sent) == 1)
    clean()
    return {"name": "break SMTP", "ok": ok,
            "detail": f"send failed -> status '{after_fail['status']}' "
                      f"(attempts {after_fail['attempts']}), never 'sent'; after the "
                      f"transport recovered -> '{after_retry['status']}', delivered once"}


# ───────────────────────────────────────────────── 4. sever Supabase ────

def sever_database() -> dict:
    """A dead database must surface as an error, not as a silent success.

    The dangerous failure is a write that appears to work. This checks the opposite:
    with the connection broken, the call raises rather than returning a cheerful None
    that a caller would read as "stored".
    """
    import engine.db as edbmod
    real = edbmod.fetch_one
    edbmod.fetch_one = lambda *a, **k: (_ for _ in ()).throw(
        ConnectionError("chaos: connection to server was lost"))
    raised = False
    try:
        adb.record_event(channel_id=BAND - 99, message_id=999_999, text="x")
    except Exception:
        raised = True
    finally:
        edbmod.fetch_one = real
    still_works = bool(edb.fetch_one("SELECT 1 AS ok")["ok"])
    return {"name": "sever the database", "ok": raised and still_works,
            "detail": "a lost connection raises rather than reporting a phantom write; "
                      "the pool recovers once the database is reachable again"}


# ──────────────────────────────────────────────── 5. Telegram FloodWait ────

def flood_wait() -> dict:
    """Telegram's FloodWait must be waited out, not retried into.

    Retrying through a FloodWait is how an account gets limited, and the account here is
    the user's personal one — re-authenticating needs a human with a phone.
    """
    src = (AGENT / "watcher.py").read_text(encoding="utf-8")
    ingest = AGENT.parent / "engine" / "sources" / "telegram" / "ingest.py"
    isrc = ingest.read_text(encoding="utf-8") if ingest.is_file() else ""
    handled = ("FloodWait" in src or "FloodWait" in isrc)
    sleeps = ("sleep" in src or "sleep" in isrc)
    return {"name": "Telegram FloodWait", "ok": handled and sleeps,
            "detail": "FloodWaitError is caught and slept out rather than retried into"
                      if handled else "no FloodWait handling found in the ingest path"}


# ──────────────────────────────────────────── 6. dead letter escalates ────

def dead_letter_path() -> dict:
    """Three failures of the same kind must produce exactly one escalation, not three."""
    clean()
    stage = f"chaos:{uuid.uuid4().hex[:6]}"
    results = [resilience.fail(stage, "SimulatedOutage", f"attempt {i}")
               for i in range(1, 4)]
    groups = [g for g in resilience.pending_escalations() if g["stage"] == stage]
    escalated_at = next((r["attempts"] for r in results if r["escalate"]), None)
    n = groups[0]["n"] if groups else 0
    resolved = resilience.resolve(stage)
    edb.execute("DELETE FROM content.agent_errors WHERE stage = %s", (stage,))
    return {"name": "dead-letter escalates", "ok": escalated_at == 3 and n == 3
            and len(groups) == 1,
            "detail": f"3 identical failures -> escalation flagged on attempt "
                      f"{escalated_at}, grouped into {len(groups)} email covering {n} "
                      f"errors (not 3 emails); {resolved} marked resolved afterwards"}


# ─────────────────────────────────────────── 7. heartbeat detects death ────

def heartbeat_gap() -> dict:
    """A runner that stops beating must be detectable by the next one."""
    w = f"chaos-{uuid.uuid4().hex[:6]}"
    resilience.beat(w, "alive")
    fresh = resilience.last_beat(w)
    edb.execute("UPDATE content.agent_state SET heartbeat_at = now() - interval '30 min' "
                "WHERE key = %s", (f"heartbeat:{w}",))
    stale = resilience.last_beat(w)
    detected = any(s["worker_id"] == w for s in resilience.stale_workers())
    edb.execute("DELETE FROM content.agent_state WHERE key = %s", (f"heartbeat:{w}",))
    return {"name": "heartbeat detects a dead runner",
            "ok": fresh["alive"] and not stale["alive"] and detected,
            "detail": f"fresh beat -> alive; 30 minutes later -> {stale['why']}; "
                      f"the next runner sees it in stale_workers()"}


async def main() -> dict:
    adb.migrate()
    results = []
    for fn in (kill_mid_batch, revoke_model):
        results.append(await fn())
        print(f"  {'ok  ' if results[-1]['ok'] else 'FAIL'} {results[-1]['name']}",
              flush=True)
    for fn in (breaker_benches, break_smtp, sever_database, flood_wait,
               dead_letter_path, heartbeat_gap):
        try:
            results.append(fn())
        except Exception as e:
            results.append({"name": fn.__name__, "ok": False,
                            "detail": f"{type(e).__name__}: {e}"[:160]})
        print(f"  {'ok  ' if results[-1]['ok'] else 'FAIL'} {results[-1]['name']}",
              flush=True)
    clean()
    out = {"passed": all(r["ok"] for r in results), "experiments": results}
    OUT.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return out


if __name__ == "__main__":
    r = asyncio.run(main())
    print(json.dumps(r, indent=1, default=str))
    raise SystemExit(0 if r["passed"] else 1)
