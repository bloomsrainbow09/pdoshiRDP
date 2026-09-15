"""IPO sub-agent — the highest-value path in the system.

An IPO post is the one message class where the numbers are the message: dates close,
lots are fixed, and a wrong price band costs real money. So this agent does three things
a generic router does not:

  * extracts the full field set rather than a summary
  * throws away any number it cannot find in the source text (`keep_grounded`)
  * counts how many OTHER channels named the same company nearby before it calls
    anything confirmed

The corroboration rule is not theoretical. IPO World — the fastest channel in the folder
by a median 12.3 hours — posted LCC GMP at ₹73 when trackers showed a high of ₹50. Speed
and accuracy are different channels' strengths, and the system's job is to use both.
"""

import re
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

import asyncio                                                    # noqa: E402
import market                                                     # noqa: E402
from base import Outcome, ask, corroboration, keep_grounded, trust

NAME = "ipo"

# Fields whose value must be quotable from the message. Anything numeric qualifies:
# these are the ones a reader cannot sanity-check on their own.
NUMERIC = ("price_band", "lot_size", "issue_size", "fresh_issue", "ofs",
           "retail_quota_pct", "gmp", "application_amount", "face_value")

SYSTEM = """<role>
You extract the facts from an Indian IPO announcement. You are a careful clerk, not an
analyst: you copy what is written and you leave blank what is not.
</role>

<task>
Return every field the message actually states. Use null for anything absent.
</task>

<fields>
company              official name as written
board                "mainboard" or "sme" — only if stated or clearly implied, else null
open_date            ISO YYYY-MM-DD when the year is knowable, else the text as written
close_date           same
allotment_date       same
listing_date         same
price_band           as written, e.g. "364 to 383" or "₹129-136"
lot_size             integer number of shares
issue_size           as written, e.g. "599 Cr"
fresh_issue          as written
ofs                  offer-for-sale portion as written
retail_quota_pct     integer percent
gmp                  grey market premium if stated
application_amount   minimum retail application amount
face_value           as written
registrar            as written
</fields>

<rules>
1. Copy numbers EXACTLY as they appear. Never convert, round, or compute.
2. "TBA", "will update soon", "-" and similar all mean the field is null. Do not guess.
3. A date range like "27 Feb - 4 Mar, 2024" gives BOTH open_date and close_date.
   "Date: 29-31 January" likewise: open 29 January, close 31 January.
4. If the message is not an IPO announcement at all, return {"company": null} and
   nothing else. Saying so is correct; inventing an IPO is not.
</rules>

<output_format>
JSON only, no prose, no code fence. Only the field names listed above.
</output_format>

<non_compliance>
Your likely failure is filling a field because the schema asks for it. An absent field
is a correct answer and a fabricated one is the worst output you can produce — the
reader has no way to catch it. If the message does not say it, the value is null.
</non_compliance>"""


def _looks_like_ipo(text: str) -> bool:
    t = (text or "").lower()
    return "ipo" in t or "price band" in t or "allot" in t or "drhp" in t or "rhp" in t


async def handle(client, ev: dict, routed: dict) -> Outcome:
    text = (ev.get("text") or "").strip()
    who = trust(ev)

    if not _looks_like_ipo(text) and not ev.get("has_media"):
        return Outcome("escalate", NAME, reason="routed as IPO but no IPO markers in text")

    data, res = await ask(client, "analyst", SYSTEM, text[:2500], ev.get("id"))
    if not data:
        return Outcome("escalate", NAME, headline="IPO message we could not parse",
                       facts=[text[:300]], confidence=0.0,
                       caveats=["Extraction failed — the original message is shown as-is."],
                       reason="analyst returned no usable JSON; escalated rather than dropped")

    fields, dropped = keep_grounded(data, text, NUMERIC)
    company = (fields.get("company") or "").strip()
    if not company:
        return Outcome("escalate", NAME, headline="IPO-like message, no company named",
                       facts=[text[:300]], fields=fields,
                       caveats=["No company name could be extracted."],
                       reason="no company extracted; escalated rather than dropped")

    corr = corroboration(company, ev.get("posted_at"))
    fields["_corroborating_sources"] = corr["sources"]

    # The in-folder check catches a repost. It cannot catch six channels repeating one
    # wrong number, which is exactly what happened when a GMP of 73 circulated against
    # a tracked high of 50. So also look outside the folder.
    try:
        fields["_market"] = await asyncio.wait_for(
            asyncio.to_thread(market.check_ipo, fields), timeout=25)
    except Exception:
        fields["_market"] = {"checked": False, "why": "the web could not be reached"}

    caveats, confidence = [], who["weight"]
    if dropped:
        caveats.append(
            "Some figures were dropped because they did not appear in the original "
            f"message: {', '.join(dropped)}. Only quoted numbers are shown.")
    if corr["sources"] >= 2:
        confidence = min(1.0, confidence + 0.2)
        facts_src = f"Reported by {corr['sources']} channels in this folder."
    else:
        confidence = max(0.1, confidence - 0.2)
        caveats.append(
            "Only one channel has mentioned this so far. Two independent sources are "
            "the bar before acting — check the registrar or NSE/BSE before you apply.")
        facts_src = "Single source so far — not yet corroborated."

    if fields.get("gmp") is not None:
        caveats.append(
            "GMP (grey market premium) is an unofficial, unregulated indication of "
            "demand. It is not a price you can trade at, and it moves daily. This "
            "folder has produced a wrong GMP before — treat it as a mood reading.")

    order = ["board", "open_date", "close_date", "price_band", "lot_size",
             "application_amount", "issue_size", "fresh_issue", "ofs",
             "retail_quota_pct", "gmp", "allotment_date", "listing_date", "registrar"]
    label = {"board": "Type", "open_date": "Opens", "close_date": "Closes",
             "price_band": "Price band", "lot_size": "Lot size (shares)",
             "application_amount": "Minimum you pay", "issue_size": "Total issue size",
             "fresh_issue": "New money to the company", "ofs": "Existing owners selling",
             "retail_quota_pct": "Share reserved for small investors",
             "gmp": "Grey market premium", "allotment_date": "Allotment result",
             "listing_date": "Lists on exchange", "registrar": "Registrar"}
    facts = [f"{label[k]}: {fields[k]}" for k in order if fields.get(k) not in (None, "")]
    facts.append(facts_src)

    known = sum(1 for k in order if fields.get(k) not in (None, ""))
    return Outcome(
        action="notify", agent=NAME,
        headline=f"IPO: {company}", fields=fields, facts=facts, caveats=caveats,
        confidence=round(confidence, 2),
        reason=(f"IPO {company}: {known} fields extracted, {corr['sources']} corroborating "
                f"source(s), source {who['tier']} ({who['title'][:40]})"))
