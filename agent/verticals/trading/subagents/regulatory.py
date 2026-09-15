"""Regulatory sub-agent — SEBI, RBI and exchange rule changes.

The rarest useful category and the only one where the default is *always deliver*. Two
things make it different from news:

  * A rule change is not an opinion. It either happened or it did not, and it applies
    whether or not the user agrees with it.
  * It usually carries a date the user must act before. That is why `regulatory` is one
    of only two intents in the taxonomy that override quiet hours.

The agent's real work is therefore not filtering — it is answering "what do I have to
do, and by when", which no channel post ever states plainly.
"""

import sys
from pathlib import Path

import sys
from pathlib import Path

# R4: this agent now lives in the vertical, but `base` (the no-drop guarantee, the
# retry-on-truncation, the Outcome shape) stays in the spine. Both paths go on:
# the project root so `core` and `engine` resolve, and agent/subagents so `base`
# resolves as the flat name it is imported by throughout.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[2]
# _HERE.parent is the vertical root, where market.py, glossary.py and signals.py sit.
# `base` and `escalation` live in core/subagents — the no-drop guarantee and the
# universal fallback are machinery every vertical inherits. _HERE.parent is this
# vertical's root, where market.py, glossary.py and signals.py sit.
for _p in (str(_ROOT), str(_ROOT / "core"), str(_ROOT / "core" / "subagents"),
           str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from base import Outcome, ask, trust  # noqa: E402

NAME = "regulatory"

SYSTEM = """<role>
You explain an Indian market regulatory change to someone who has never bought a share.
</role>

<task>
Say what changed, who it applies to, what the reader must actually DO, and by when.
</task>

<output_format>
JSON only, no prose, no code fence:
{"regulator": "SEBI" | "RBI" | "NSE" | "BSE" | "MCX" | "Government" | "other" | null,
 "what_changed": "<one plain-English sentence>",
 "who_it_affects": "<one short phrase: e.g. 'anyone who trades options'>",
 "action_needed": "<what the reader must do, or 'Nothing — this is for information'>",
 "deadline": "<the date by which they must act, or null>",
 "affects_retail": true|false,
 "confidence": 0.0-1.0}
</output_format>

<rules>
0. SHORT SENTENCES. Every sentence you write must be under 20 words. Two short
   sentences always beat one long one. This is measured: the finished email is scored
   for reading ease and a single long clause-heavy sentence fails it.
1. Expand every abbreviation the first time you use it: F&O is "futures and options",
   KYC is "the identity check your broker keeps on file", T+0 is "same-day settlement".
1b. NEVER GUESS AN EXPANSION. If you do not know what the letters stand for, say what
   the thing DOES in the message's own terms and leave the letters alone: "ASM, a list
   the exchange puts risky shares on". Do not invent words to fit the initials. An
   invented expansion was produced once — "ASM" was rendered as "Asset Securitization
   Mechanism" when it means Additional Surveillance Measure — and a confident wrong
   expansion is worse than the bare abbreviation, because the reader cannot tell.
2. `action_needed` must be concrete. "Be aware of the change" is not an action.
   If there is genuinely nothing to do, say exactly: "Nothing — this is for information".
3. Never state a deadline the message does not give.
4. affects_retail is false only for rules that apply purely to institutions, brokers or
   exchanges with no retail consequence.
</rules>

<non_compliance>
Your likely failure is restating the announcement in its own jargon. The reader does not
know what "peak margin", "ASM framework" or "T+1" mean. If your answer contains a term
you have not expanded in the same sentence, you have not done the task.
</non_compliance>"""


async def handle(client, ev: dict, routed: dict) -> Outcome:
    text = (ev.get("text") or "").strip()
    who = trust(ev)

    data, _ = await ask(client, "analyst", SYSTEM, text[:2200], ev.get("id"))
    if not data or not str(data.get("what_changed") or "").strip():
        # Regulatory is always material. An unparsed one is delivered whole, not held.
        return Outcome("notify", NAME, headline="Rule change we could not summarise",
                       facts=[text[:600]], confidence=0.2,
                       caveats=["We could not summarise this, so the original is shown "
                                "in full. Regulatory changes are always delivered."],
                       reason="summary failed; delivered raw — regulatory is never held")

    what = str(data["what_changed"]).strip()
    action = str(data.get("action_needed") or "").strip()
    facts = [what]
    if data.get("who_it_affects"):
        facts.append(f"Who this applies to: {data['who_it_affects']}")
    facts.append(f"What you need to do: {action or 'Nothing — this is for information'}")
    if data.get("deadline"):
        facts.append(f"Deadline: {data['deadline']}")
    facts.append(f"Source: {who['title'][:60]} ({who['tier'] or 'untiered'})")

    caveats = ["Rule changes get garbled in forwarding. Before acting, check the "
               "regulator's own site — sebi.gov.in, rbi.org.in, or your exchange."]
    if not data.get("affects_retail", True):
        caveats.append("This appears to apply to institutions rather than individual "
                       "investors, so it probably does not require anything from you.")

    reg = data.get("regulator") or "Regulator"
    return Outcome("notify", NAME, headline=f"{reg}: {what[:80]}",
                   fields=data, facts=facts, caveats=caveats,
                   confidence=round(float(data.get("confidence") or 0.6), 2),
                   reason=f"regulatory change from {reg}; "
                          f"deadline={data.get('deadline') or 'none stated'} — "
                          f"always delivered")
