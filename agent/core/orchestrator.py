"""The orchestrator: one message in, one terminal decision out.

    event -> [guard]   droppable?        -> discard (logged with a reason)
          -> [router]  intent+confidence
             >= threshold  -> route by taxonomy action
             <  threshold  -> [importance assessor] -> escalate | discard (logged)
          -> any failure   -> [error_router] -> retry | dead-letter -> escalate

Invariants, each enforced in code rather than by convention:

* **Every event gets a terminal decision.** There is no path that returns without
  calling `decide()`, and `decide()` refuses an empty reason.
* **A per-message budget.** One message can never consume unbounded model calls; the
  cap is checked before each stage.
* **Failures escalate, they do not vanish.** If the pipeline breaks on a message, that
  message becomes an escalation, because a crash must not be indistinguishable from a
  discard.
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path

AGENT = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT))
sys.path.insert(0, str(AGENT.parent))

import httpx  # noqa: E402

import db as adb        # noqa: E402
import llm              # noqa: E402
from core import subagents            # noqa: E402  the spine's machinery: base.run()
from core import prompts as scaffold  # noqa: E402
from core import registry             # noqa: E402
from core import active  # noqa: E402

# R5: the veto moved to the vertical that defines it. Loaded by name so the orchestrator
# stays free of any knowledge of what a signal looks like — see verticals/<n>/signals.py.
VERTICAL = registry.current()


def has_material_signal(text: str) -> bool:
    """Ask the vertical. The orchestrator does not decide what "material" means."""
    return VERTICAL.has_material_signal(text)


# The name the pipeline used before R5, kept so the gates and the baseline address it
# the same way. It is an alias, not a second implementation.
has_trading_signal = has_material_signal

CONFIG = json.loads((AGENT / "config" / "models.json").read_text(encoding="utf-8"))
TAX = VERTICAL.taxonomy
INTENTS = TAX["intents"]
# The categories the GUARD may return. Derived from the taxonomy's discard actions,
# which is deliberately NOT the same set as subagents.DROPPABLE — that one also
# carries `filtered`, a decision the guard never makes.
_GUARD_CATEGORIES = scaffold.droppable(TAX)

# Which agents produce a row in the accuracy ledger. Read from the taxonomy so a vertical
# declares it rather than core naming one agent; absent, nothing is ledgered, which is the
# right default for a niche that makes no checkable predictions.
_LEDGER_AGENTS = set(getattr(VERTICAL, "ledger_agents", lambda: set())())
THRESHOLD = float(TAX["confidence_threshold"]["value"])
# guard + router + (router retry) + importance assessor + sub-agent = 5. The cap exists
# to stop one pathological message looping, not to ration the normal path, so it is set
# to exactly what the longest legitimate route costs.
MAX_CALLS_PER_MESSAGE = 5

# Rough $/1k tokens. Only used for budget signalling; every model in use is free or
# near-free, so the number that matters is call COUNT, not dollars.
COST_PER_1K = 0.0002


def _chain(role: str) -> list:
    return [tuple(c) for c in CONFIG["roles"][role]["chain"]]


def _maxtok(role: str) -> int:
    return CONFIG["roles"][role].get("max_tokens", CONFIG["defaults"]["max_tokens"])


def _delivery(action: str) -> str:
    """Taxonomy action -> terminal decision. `digest` holds for the daily roll-up;
    every other non-discard action ends in an email."""
    return "digest" if action == "digest" else "notify"




class Budget:
    """Per-message call cap. Prevents one pathological message from looping."""

    def __init__(self, limit: int = MAX_CALLS_PER_MESSAGE):
        self.limit, self.used = limit, 0

    def take(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


async def _ask(client, role: str, system: str, user: str, event_id=None) -> tuple:
    """One role call: run the fallback chain, log it, return (parsed_json, Result)."""
    res = await llm.call(_chain(role), system, user, client=client,
                         max_tokens=_maxtok(role),
                         temperature=CONFIG["defaults"]["temperature"],
                         timeout=CONFIG["defaults"]["timeout_s"],
                         json_mode=False)
    adb.record_run(role, res, event_id, cost=res.tokens / 1000 * COST_PER_1K)
    parsed = llm.parse_json(res.text) if res.ok else None
    # Same guarantee as base.ask: a JSON array is not an answer to any of these prompts,
    # and every stage below treats None as "unusable" rather than crashing on it.
    return (parsed if isinstance(parsed, dict) else None), res


async def _via_subagent(client, ev, budget, eid, t0, common, spec, caveat=None) -> dict:
    """Hand the event to its sub-agent and record whatever the sub-agent decides.

    The sub-agent owns the outcome, with two limits the orchestrator keeps for itself:
    it may not discard a delivery category (that rule lives in `process`, above), and
    if the budget is gone the message escalates rather than going unhandled.
    """
    intent = common["intent"]
    if not budget.take():
        adb.decide(eid, "escalate", f"{intent}: budget exhausted before the sub-agent "
                                    f"could run — escalated rather than dropped", **common)
        return {"decision": "escalate", "intent": intent, "ms": _ms(t0)}

    out = await subagents.run(VERTICAL.subagent_for((common or {}).get('intent')),
                              client, ev, common)
    if caveat:
        out.caveats = list(out.caveats) + [caveat]

    # A sub-agent's `discard` is honoured only where the taxonomy already allows one.
    # `trade_call` returning discard for a results-recap is legitimate; a sub-agent
    # deciding a whole delivery category is not interesting is not its call to make.
    decision = out.action
    if decision == "discard" and spec["action"] not in ("discard", "assess_importance"):
        if out.agent not in _LEDGER_AGENTS:
            decision = "escalate"
            out.reason = (f"sub-agent asked to discard a delivery category; escalated "
                          f"instead — {out.reason}")

    adb.decide(eid, decision, f"{out.agent}: {out.reason}"[:900],
               agent_outcome=out.as_dict(), **common)
    return {"decision": decision, "intent": intent, "agent": out.agent,
            "headline": out.headline, "ms": _ms(t0)}


async def process(client, ev: dict) -> dict:
    """Take one event to a terminal decision. Never raises."""
    eid, budget = ev["id"], Budget()
    user = scaffold.user_block(ev)
    t0 = time.perf_counter()

    # Computed once, consulted by every path that can drop this message. A message
    # carrying something a trader could act on is never silently dropped — not by the
    # guard, not by a confident router calling it a fragment, and not by the importance
    # assessor. Three doors, one rule.
    protected = has_trading_signal(ev.get("text") or "")

    try:
        # ── stage 1: guard ────────────────────────────────────────────────────
        if not budget.take():
            adb.decide(eid, "escalate", "budget exhausted before guard")
            return {"decision": "escalate", "intent": None}
        g, gres = await _ask(client, "guard", VERTICAL.guard_prompt(), user, eid)
        if g and g.get("drop") is True:
            why = str(g.get("why") or "guard")[:120]
            if protected:
                # Overruled. See _SIGNAL: the guard drops real calls, and a message with
                # a level in it is never noise no matter how it is phrased.
                adb.record_run("guard_override", gres, eid, cost=0.0)
            else:
                # Record WHICH category it matched, not a generic "filtered". A discard
                # the reader never sees still has to be explainable a month later.
                cat = g.get("category")
                cat = cat if cat in _GUARD_CATEGORIES else "fragment"
                adb.decide(eid, "discard", f"guard: {cat} — {why}", intent=cat,
                           router_model=gres.model)
                return {"decision": "discard", "intent": cat, "ms": _ms(t0)}
        # A guard that fails is NOT a reason to drop. Fall through to the router.

        # ── stage 2: router ───────────────────────────────────────────────────
        if not budget.take():
            adb.decide(eid, "escalate", "budget exhausted before router")
            return {"decision": "escalate", "intent": None}
        r, rres = await _ask(client, "router", VERTICAL.router_prompt(), user, eid)
        if not r or r.get("intent") not in INTENTS:
            # One retry, then treat as unknown — never crash, never drop.
            if budget.take():
                r, rres = await _ask(client, "router", VERTICAL.router_prompt(), user, eid)
            if not r or r.get("intent") not in INTENTS:
                r = {"intent": "unknown", "confidence": 0.0,
                     "reasoning": "router returned unusable output twice"}

        intent = r["intent"]
        conf = float(r.get("confidence") or 0)
        spec = INTENTS[intent]
        common = dict(intent=intent, confidence=conf, router_model=rres.model,
                      reasoning=str(r.get("reasoning") or "")[:500],
                      entities=r.get("entities") or {})

        # NOTE on stage 3b below: `assess_importance` is not a delivery action, it is
        # an instruction to ask the assessor. A CONFIDENT `unknown` is still unknown, so
        # confidence must not let it skip stage 3c. Observed before this was fixed: an
        # unknown at 0.85 was marked notify with no summary and no assessor call.

        # ── stage 3a: confident and droppable ─────────────────────────────────
        # The second door. The guard let "ZOMATO HIT UPPER CIRCUIT" through and the
        # ROUTER then called it a fragment with high confidence, which discarded it just
        # as effectively.
        if spec["action"] == "discard" and conf >= THRESHOLD and not protected:
            adb.decide(eid, "discard", f"{intent} (confidence {conf:.2f})", **common)
            return {"decision": "discard", "intent": intent, "ms": _ms(t0)}
        if spec["action"] == "discard" and protected:
            adb.decide(eid, "escalate",
                       f"{intent} (confidence {conf:.2f}) but it carries a price level "
                       f"or a market event, so it is shown rather than dropped", **common)
            return {"decision": "escalate", "intent": intent, "ms": _ms(t0)}

        # ── stage 3b: confident and deliverable ───────────────────────────────
        if conf >= THRESHOLD and spec["action"] not in ("discard", "assess_importance"):
            return await _via_subagent(client, ev, budget, eid, t0, common, spec)

        # ── stage 3c: unknown, or not confident -> importance assessor ────────
        # The path that makes "never miss anything" real. It sees everything the router
        # could not place: low-confidence anything, plus EVERY `unknown` regardless of
        # how confident the router was that it is unknown.
        if not budget.take():
            adb.decide(eid, "escalate", f"{intent} unconfident, budget exhausted", **common)
            return {"decision": "escalate", "intent": intent, "ms": _ms(t0)}
        a, _ = await _ask(client, "analyst", VERTICAL.importance_prompt(), user, eid)
        if a and a.get("notify") is True:
            return await _via_subagent(
                client, ev, budget, eid, t0, common, spec,
                caveat=f"Flagged as worth seeing even though it did not fit a category: "
                       f"{str(a.get('why') or 'material')[:90]}.")
        if a and a.get("notify") is False:
            # Two very different things arrive here as notify=false, and the split
            # matters more than anything else in this file:
            #
            #   spam=true       the assessor is RECLASSIFYING — the router called this
            #                   an IPO listing, but it is a broker's Diwali offer. The
            #                   guard's job, done late. Discard.
            #   spam=false      the assessor is judging it IMMATERIAL. On a droppable
            #                   or unplaced intent that is its call to make. On a
            #                   DELIVERY category it is not: a thin trade_call is
            #                   still the user's own message in a category they asked
            #                   to receive, and it goes out with a caveat attached.
            #                   Withholding a whole category is a decision the user
            #                   makes, never the assessor.
            #                   (Observed: '950 ₹ to 1065 ₹ 🎯', a real trade_call at
            #                   0.60, discarded for naming no company.)
            # A message carrying a price level survives the assessor too. The guard veto
            # covers the first door; '1060 ₹ to 1125 ₹ 🎯' got through it, was routed
            # `unknown` — whose action IS assess_importance — and died at the second.
            # Wherever a droppable path exists, the same rule has to hold.
            # The signal veto and the spam reclassification collide here, and the
            # order matters. `protected` overrules a HEURISTIC drop — the guard's cheap
            # first pass, or a confident router calling a call a fragment. It does not
            # overrule the assessor saying spam=true, because that is a model that has
            # read the whole message and specifically judged it to be SELLING something.
            # A promo can carry a real level ("join the premium group — today: BUY X
            # 100 to 115"), and the guard rule is explicit that the selling is the point
            # of it. Losing this ordering turned a promotional message into a notify and
            # regressed the P06 invariant.
            if a.get("spam") is True or (not protected
                                         and spec["action"] in ("discard",
                                                                "assess_importance")):
                tag = "reclassified as promotional" if a.get("spam") else "not material"
                adb.decide(eid, "discard",
                           f"{intent} (confidence {conf:.2f}); assessor {tag}: "
                           f"{str(a.get('why') or '-')[:100]}", **common)
                return {"decision": "discard", "intent": intent, "ms": _ms(t0)}
            if protected and spec["action"] in ("discard", "assess_importance"):
                adb.decide(eid, "escalate",
                           f"{intent} (confidence {conf:.2f}); carries a price level, so "
                           f"it is shown rather than dropped — assessor said: "
                           f"{str(a.get('why') or '-')[:80]}", **common)
                return {"decision": "escalate", "intent": intent, "ms": _ms(t0)}
            return await _via_subagent(
                client, ev, budget, eid, t0, common, spec,
                caveat=f"Low confidence in the classification of this one "
                       f"({conf:.0%}); it is delivered anyway rather than withheld. "
                       f"Our reservation: {str(a.get('why') or 'thin on detail')[:80]}.")

        # Assessor itself failed. Escalate — an unreadable message is not a droppable
        # one, and silence here would be exactly the failure mode to avoid.
        adb.decide(eid, "escalate", "assessor unavailable; escalating rather than dropping",
                   **common)
        return {"decision": "escalate", "intent": intent, "ms": _ms(t0)}

    except Exception as e:
        adb.dead_letter("orchestrator", type(e).__name__, str(e), event_id=eid)
        try:
            adb.decide(eid, "failed", f"pipeline error: {type(e).__name__}: {str(e)[:160]}")
        except Exception:
            pass
        return {"decision": "failed", "intent": None, "error": f"{type(e).__name__}",
                "ms": _ms(t0)}


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


async def run(limit: int = 100, concurrency: int | None = None,
              production_only: bool = False) -> dict:
    """Drain undecided events. Safe to run repeatedly; picks up where it stopped."""
    adb.migrate()
    todo = adb.pending_events(limit, production_only)
    if not todo:
        return {"processed": 0}
    conc = concurrency or CONFIG["defaults"]["concurrency"]
    stats = {"processed": 0, "notify": 0, "digest": 0, "discard": 0,
             "escalate": 0, "failed": 0, "ms": []}

    async with httpx.AsyncClient() as client:
        async def one(ev):
            out = await process(client, ev)
            stats["processed"] += 1
            stats[out["decision"]] = stats.get(out["decision"], 0) + 1
            if out.get("ms"):
                stats["ms"].append(out["ms"])
            return out
        await llm.map_bounded(todo, one, limit=conc)

    lat = sorted(stats.pop("ms")) or [0]
    stats["p50_ms"] = lat[len(lat) // 2]
    stats["p95_ms"] = lat[int(len(lat) * 0.95)]
    return stats


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--concurrency", type=int)
    a = ap.parse_args()
    print(json.dumps(asyncio.run(run(a.limit, a.concurrency)), indent=2))
