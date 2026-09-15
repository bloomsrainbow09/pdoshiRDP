"""News sub-agent — a materiality filter, not a summariser.

Most "news" on these channels is not actionable: a stock moved 2%, a sector is "in
focus", somebody has a view. Delivering all of it would train the user to ignore the
inbox, which is the same outcome as delivering none of it.

So this agent asks one question — *would a specific, checkable thing have changed?* —
and it answers it about a named company. News with no company attached is market colour;
it goes to the digest rather than the inbox.

Materiality here is deliberately conservative in one direction only: when the model
cannot decide, the message is escalated, never dropped.
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

from base import Outcome, ask, keep_grounded, trust  # noqa: E402

NAME = "news"
NUMERIC = ("amount", "percent")

SYSTEM = """<role>
You judge whether a piece of Indian stock-market news is MATERIAL: would it plausibly
change what a share is worth, or what a holder of it would do?
</role>

<material>
  order wins and contract awards with a value
  acquisitions, mergers, stake sales, promoter pledge changes
  board decisions: dividend, bonus, split, buyback, fundraise
  results with actual numbers
  regulatory action against a named company
  management change at CEO/CFO/promoter level
  a plant, licence, approval or ban that affects operations
</material>

<not_material>
  a share moved up or down today
  a sector is "in focus" or "in action"
  broker targets and analyst opinions with no new fact
  a repeat of something already widely known
  general economic commentary with no named company
</not_material>

<output_format>
JSON only, no prose, no code fence:
{"company": "<named company or null>",
 "material": true|false,
 "what_changed": "<one plain-English sentence: the new FACT, not the reaction>",
 "why_it_matters": "<one plain-English sentence a person with no market knowledge
                     understands; expand any jargon in the same sentence>",
 "amount": "<the money figure if one is stated, else null>",
 "percent": "<the percentage if one is stated, else null>",
 "confidence": 0.0-1.0}
</output_format>

<length>
`what_changed` must be UNDER 18 WORDS and must be a single sentence. It is used verbatim
as a line in a daily round-up that is scored for reading ease, and one long clause-heavy
summary drags the whole email below the bar. State the new fact and stop.
</length>

<non_compliance>
Two failures, and they pull in opposite directions:
  1. Calling a price move "news". A share rising is not a fact about the business.
  2. Suppressing a real corporate event because it is small. A ten-crore order for a
     small company is material; size is relative to the company, not absolute.
If you cannot tell, set confidence below 0.5 and let it be reviewed rather than guessing.
</non_compliance>"""


async def handle(client, ev: dict, routed: dict) -> Outcome:
    text = (ev.get("text") or "").strip()
    who = trust(ev)

    data, _ = await ask(client, "analyst", SYSTEM, text[:2200], ev.get("id"))
    if not data or "material" not in data:
        return Outcome("escalate", NAME, headline="Market news we could not assess",
                       facts=[text[:300]], confidence=0.0,
                       caveats=["Could not judge whether this matters — shown so you "
                                "can decide."],
                       reason="materiality check unusable; escalated rather than dropped")

    fields, dropped = keep_grounded(data, text, NUMERIC)
    company = (fields.get("company") or "").strip()
    conf = float(data.get("confidence") or 0)
    material = bool(data.get("material"))
    what = str(fields.get("what_changed") or "").strip()
    why = str(fields.get("why_it_matters") or "").strip()

    if conf < 0.5:
        return Outcome("escalate", NAME,
                       headline=f"Possible news: {company or 'unnamed company'}",
                       fields=fields, facts=[what or text[:300]],
                       caveats=["We were not sure this mattered, so it is shown rather "
                                "than filed away."],
                       confidence=conf,
                       reason=f"materiality uncertain ({conf:.2f}); escalated, not dropped")

    if not material:
        return Outcome("digest", NAME, headline=what[:90] or "Market colour",
                       fields=fields, facts=[what] if what else [],
                       confidence=conf,
                       reason=f"not material ({conf:.2f}) — held for the daily digest "
                              f"rather than emailed")

    if not company:
        return Outcome("digest", NAME, headline=what[:90] or "Market news",
                       fields=fields, facts=[f for f in (what, why) if f],
                       confidence=conf,
                       reason="material but no company named — market colour, digested")

    facts = [f for f in (what, why) if f]
    if fields.get("amount"):
        facts.append(f"Amount involved: {fields['amount']}")
    if fields.get("percent"):
        facts.append(f"Size of the change: {fields['percent']}")
    facts.append(f"Source: {who['title'][:60]} ({who['tier'] or 'untiered'})")

    caveats = ["This is a channel post, not a company filing. Confirm anything you plan "
               "to act on against the exchange announcement on nseindia.com or bseindia.com."]
    if (who["weight"] or 0) < 0.5:
        caveats.append("Lower-tier source — treat as a pointer to check, not a fact.")
    if dropped:
        caveats.append(f"Figures not present in the original message were dropped: "
                       f"{', '.join(dropped)}.")

    return Outcome("notify", NAME, headline=f"{company}: {what[:80]}",
                   fields=fields, facts=facts, caveats=caveats,
                   confidence=round(min(1.0, conf * (0.6 + 0.4 * who["weight"])), 2),
                   reason=f"material news on {company} (confidence {conf:.2f}), "
                          f"source {who['tier']}")
