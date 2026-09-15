"""Escalation sub-agent — the UNCLASSIFIED path.

Everything that no other agent could place ends here: the ~30% of substantive traffic
the taxonomy calls `unknown`, plus any sub-agent that failed. This is the last stop
before a message would be lost, so it has one rule the others do not — **it cannot
return `discard`**. There is no code path out of this file that drops a message.

Its job is not classification. It is to make an unclassifiable message *readable*: say
what it appears to be about in plain English, and be honest that the system did not
understand it. A message the user can read and dismiss in three seconds has cost them
almost nothing. A message they never saw may have cost them a lot.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from base import Outcome, ask, trust  # noqa: E402

NAME = "escalation"

SYSTEM = """<role>
An automated system could not classify this market message. You write the one sentence
that lets a person decide in three seconds whether to read it.
</role>

<output_format>
JSON only, no prose, no code fence:
{"about": "<one plain-English sentence: what this message appears to be about>",
 "entities": ["<company, index or scheme names mentioned>"],
 "time_sensitive": true|false,
 "worth_reading": true|false,
 "language": "english" | "hindi" | "gujarati" | "mixed" | "other"}
</output_format>

<rules>
1. Plain English only, whatever the source language. Roughly 15% of this folder is
   Gujarati and translating is part of the job, not an excuse.
2. `about` describes the message; it does not evaluate it. No advice, no opinion.
3. time_sensitive is true if the message references a deadline, a date, a market
   session, or anything that expires.
4. Never say the message is unimportant. You are the last check before it is lost;
   worth_reading=false means "probably routine", not "safe to delete".
</rules>

<non_compliance>
Your likely failure is producing a summary as opaque as the original — repeating its
jargon or its abbreviations back. If a reader who has never bought a share could not
follow your sentence, rewrite it.
</non_compliance>"""


async def handle(client, ev: dict, routed: dict) -> Outcome:
    text = (ev.get("text") or "").strip()
    who = trust(ev)
    intent = (routed or {}).get("intent") or "unknown"

    data, _ = await ask(client, "analyst", SYSTEM, text[:2000], ev.get("id"))

    if not data or not str(data.get("about") or "").strip():
        # The floor of the whole system. Even total failure ships the raw message.
        return Outcome(
            "escalate", NAME, headline="Unclassified message",
            facts=[text[:500] or "(no text — media only)",
                   f"Source: {who['title'][:60]} ({who['tier'] or 'untiered'})"],
            caveats=["The system could not read this one at all. It is shown exactly as "
                     "it arrived so that nothing is lost."],
            confidence=0.0,
            reason="escalation summary failed; raw message delivered — never dropped")

    about = str(data["about"]).strip()
    ents = [str(e) for e in (data.get("entities") or []) if str(e).strip()][:6]

    facts = [about]
    if ents:
        facts.append("Mentions: " + ", ".join(ents))
    if data.get("language") and data["language"] != "english":
        facts.append(f"Original language: {data['language']} — summarised in English.")
    facts.append(f"Source: {who['title'][:60]} ({who['tier'] or 'untiered'})")

    caveats = ["The system could not fit this into a known category, so it is passed "
               "through with a plain-English summary rather than filed away."]
    if data.get("time_sensitive"):
        caveats.append("This looks time-sensitive — it mentions a date or a deadline.")

    return Outcome(
        "escalate", NAME,
        headline=("Worth a look: " if data.get("worth_reading") else "FYI: ") + about[:80],
        fields={"entities": ents, "time_sensitive": bool(data.get("time_sensitive")),
                "language": data.get("language"), "routed_as": intent},
        facts=facts, caveats=caveats,
        confidence=0.5 if data.get("worth_reading") else 0.3,
        reason=(f"unclassified ({intent}) summarised for review; "
                f"time_sensitive={bool(data.get('time_sensitive'))} — escalated, "
                f"never dropped"))
