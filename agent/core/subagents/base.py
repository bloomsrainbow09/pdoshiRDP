"""The sub-agent contract.

One focused agent per intent family. Each has the same shape and the same three
promises, which are enforced here rather than repeated in every file:

  1. **It never drops.** A sub-agent that cannot parse its input returns `escalate`.
     Escalation costs the user five seconds; silence costs them the alert. Every exit
     path in `run()` produces an Outcome — exceptions included.
  2. **It never invents.** Extraction is verified against the source text before it is
     returned (`keep_grounded`), because a hallucinated price band is worse than a
     missing one: the user cannot tell it is wrong.
  3. **It knows who is talking.** Channel trust from `tiers.json` is an input to every
     decision. A tier1 claim and a tier3 claim are not equal evidence, and the
     difference is measured, not assumed — see verticals/trading/CHANNEL-RANKING.md.
"""

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# parents[1] is core/, which is where models.json and the pipeline modules live.
AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT))
sys.path.insert(0, str(AGENT.parent))

import db as adb        # noqa: E402
import llm              # noqa: E402
from core import active       # noqa: E402
from engine import db as edb  # noqa: E402

CONFIG = json.loads((AGENT / "config" / "models.json").read_text(encoding="utf-8"))
# Channel trust ranking. Every vertical has one — the CONCEPT is generic, the
# placements are not — so this resolves through the active vertical rather than
# naming trading, which is what it did before R4.
TIERS = active.data("tiers.json")
PLACEMENTS = TIERS["placements"]

TIER_WEIGHT = {"tier1": 1.0, "tier2": 0.7, "tier3": 0.4, "newly-added": 0.3,
               "useless": 0.1, None: 0.3}


@dataclass
class Outcome:
    """What a sub-agent returns. `action` is the only field the orchestrator must read."""
    action: str                                  # notify | digest | escalate | discard
    agent: str
    headline: str = ""
    fields: dict = field(default_factory=dict)   # structured extraction
    facts: list = field(default_factory=list)    # plain-English bullets for the email
    caveats: list = field(default_factory=list)  # what the reader must not assume
    confidence: float = 0.0
    reason: str = ""                             # always populated; never a silent exit

    def as_dict(self) -> dict:
        return {"action": self.action, "agent": self.agent, "headline": self.headline,
                "fields": self.fields, "facts": self.facts, "caveats": self.caveats,
                "confidence": self.confidence, "reason": self.reason}


# EXACTLY the universal half of the original r"(?i)\b(ltd|limited|ipo|pvt|private)\b".
# "inc", "corp", "plc" and "llc" are tempting and were briefly added here — and that
# changed behaviour: "Incredible Corp" started stripping to "Incredible". A refactor
# does not get to improve the list. Trading contributes "ipo" via name_suffixes().
_GENERIC_SUFFIXES = ("ltd", "limited", "pvt", "private")


def _suffix_pattern() -> str:
    """Corporate suffixes to strip before comparing two company names."""
    extra = ()
    try:
        from core import registry
        extra = tuple(getattr(registry.current(), "name_suffixes", lambda: ())())
    except Exception:
        pass
    return r"(?i)\b(" + "|".join(_GENERIC_SUFFIXES + tuple(extra)) + r")\b"


def trust(ev: dict) -> dict:
    """What we know about the channel that said this — measured, not assumed."""
    cid = str(ev.get("channel_id") or "")
    p = PLACEMENTS.get(cid) or {}
    tier = p.get("tier") or ev.get("tier")
    return {"tier": tier, "weight": TIER_WEIGHT.get(tier, 0.3),
            "category": p.get("category"),
            "title": p.get("title") or ev.get("channel_title") or "unknown channel",
            "note": p.get("note") or ""}


def chain(role: str) -> list:
    return [tuple(c) for c in CONFIG["roles"][role]["chain"]]


async def ask(client, role: str, system: str, user: str, event_id=None):
    """One model call through the role's cross-provider chain. Logged, never raises.

    Retries ONCE at double the budget when the reply was truncated and would not parse.
    A reasoning model that runs out of tokens mid-JSON returns ok=True with a valid-
    looking prefix, so this failure is invisible unless it is checked for: it cost 4 of
    12 IPO extractions in P7 before the budget was raised. The retry stays as a guard
    because a longer message or a wider schema would reintroduce it.
    """
    budget = CONFIG["roles"][role].get("max_tokens", CONFIG["defaults"]["max_tokens"])
    for attempt in (1, 2):
        res = await llm.call(chain(role), system, user, client=client,
                             max_tokens=budget * attempt,
                             temperature=CONFIG["defaults"]["temperature"],
                             timeout=CONFIG["defaults"]["timeout_s"], json_mode=False)
        adb.record_run(role, res, event_id, cost=res.tokens / 1000 * 0.0002)
        parsed = llm.parse_json(res.text) if res.ok else None
        # A model that answers with a JSON ARRAY is not usable here, and must not reach
        # a caller that will do `.get()` on it. The soak caught this live: six
        # `AttributeError: 'list' object has no attribute 'items'` in the IPO agent and
        # two in the trade-call agent. Nothing was lost — base.run() turned each into an
        # escalation — but every agent already has a correct "unparseable" branch, and
        # this routes to it instead of crashing into the safety net.
        if not isinstance(parsed, dict):
            parsed = None
        if parsed is not None or not (res.ok and res.truncated):
            return parsed, res
    return None, res


# ---------------------------------------------------------------- grounding ----

_NUM = re.compile(r"\d[\d,]*\.?\d*")


def _digits(s) -> set:
    """Every number in a string, normalised. '₹14,880' and '14880' are the same fact."""
    return {n.replace(",", "").rstrip(".0") or "0" for n in _NUM.findall(str(s))}


def keep_grounded(fields: dict, source: str, numeric_keys: tuple) -> tuple:
    """Drop any extracted NUMBER that does not appear in the source text.

    A model asked for a price band will supply one whether or not the message contains
    it, and a plausible invented figure is the single most dangerous output this system
    can produce — the reader has no way to catch it. So numbers must be quotable from
    the message. Returns (kept_fields, dropped_keys).
    """
    src = _digits(source)
    kept, dropped = {}, []
    for k, v in fields.items():
        if v in (None, "", [], {}):
            continue
        if k in numeric_keys:
            want = _digits(v)
            if want and not want.issubset(src):
                dropped.append(k)
                continue
        kept[k] = v
    return kept, dropped


def corroboration(company: str, posted_at, within_hours: int = 48) -> dict:
    """How many OTHER channels said the same company's name near the same time.

    plan/04-decision-rules.md Hard Gate 1 wants two independent sources before acting on
    an IPO. There is a concrete precedent: IPO World posted LCC GMP at ₹73 when trackers
    showed a high of ₹50. One channel is a claim; two are evidence.
    """
    # Corporate suffixes are universal; the niche-specific ones are not. A vertical adds
    # its own via `name_suffixes()` — trading contributes "ipo", because channels write
    # "Sonaselection IPO" and it must still match "Sonaselection" from another channel.
    name = re.sub(_suffix_pattern(), " ", company or "").strip()
    name = re.sub(r"\s+", " ", name)
    if len(name) < 4:
        return {"sources": 0, "channels": [], "term": name}
    rows = edb.fetch_all(
        """SELECT DISTINCT i.channel_id, coalesce(c.title, i.channel_id::text) title
             FROM content.items i
             LEFT JOIN content.source_channels c ON c.id = i.channel_id
            WHERE i.text ILIKE %s
              AND (%s::timestamptz IS NULL
                   OR i.posted_at BETWEEN %s::timestamptz - (%s || ' hours')::interval
                                      AND %s::timestamptz + (%s || ' hours')::interval)
            LIMIT 12""",
        (f"%{name}%", posted_at, posted_at, within_hours, posted_at, within_hours))
    return {"sources": len(rows), "term": name,
            "channels": [r["title"][:48] for r in rows]}


async def run(agent_module, client, ev: dict, routed: dict | None = None) -> Outcome:
    """Invoke a sub-agent so that no failure can become a dropped message."""
    try:
        out = await agent_module.handle(client, ev, routed or {})
        if not isinstance(out, Outcome) or not out.reason:
            return Outcome("escalate", agent_module.NAME,
                           reason="sub-agent returned no usable outcome")
        return out
    except Exception as e:
        adb.dead_letter(f"subagent:{getattr(agent_module, 'NAME', '?')}",
                        type(e).__name__, str(e), event_id=ev.get("id"))
        return Outcome("escalate", getattr(agent_module, "NAME", "?"),
                       reason=f"sub-agent raised {type(e).__name__}; escalated, not dropped")
