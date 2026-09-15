"""Sample fixtures for the TRADING vertical.

`core/samples.py` owns the MECHANISM — finding a real archived message that demonstrates
each template, caching the choice, marking the result `[SAMPLE]`, keeping it off the live
rate limit and out of the real alert stream. None of that is niche-specific.

What IS niche-specific is everything here: which archived message demonstrates which
template, what a company name looks like, and when the market is open. A vertical without
samples simply has no `samples.py`, and `core/samples.py` finds nothing to send.

The patterns are patterns rather than fixed message ids on purpose: the archive grows, and
a hard-coded id rots the first time a channel is re-synced.
"""

import re
import sys
from datetime import datetime, time as dtime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (str(_ROOT), str(_HERE), str(_ROOT / "core"), str(_ROOT / "core" / "delivery"),
           str(_ROOT / "core" / "subagents")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.delivery import policy            # noqa: E402

_subagents = None


def agents():
    """This vertical's agent modules, loaded lazily to avoid an import cycle."""
    global _subagents
    if _subagents is None:
        from core import registry
        _subagents = registry.load("trading")._subagents
    return _subagents


MARKET_OPEN, MARKET_CLOSE = dtime(9, 15), dtime(15, 30)

# A sample must NAME a company. Matching on keywords alone produced "Allotment out —
# your IPO: check now": the plumbing demonstrated, the product not. A generic GMP table
# covering nine IPOs at once is a real message and a bad advertisement for what a live
# alert looks like.
_NAMED = re.compile(r"(?<![A-Za-z])[A-Z][A-Za-z&.\-]{3,}"
                    r"(?:\s+[A-Z][A-Za-z&.\-]{2,}){0,3}\s+"
                    r"(?:Ltd|Limited|IPO)(?![A-Za-z])")

# Which archived message demonstrates which template. Patterns, not fixed message ids:
# the archive grows and a hard-coded id rots the first time a channel is re-synced.
WANTED = {
    "ipo_new": (agents().ipo, re.compile(
        r"(?is)(?=.*price\s*band)(?=.*lot\s*size)(?=.*(open|issue\s*date))")),
    # The company name must sit NEXT TO the keyword. Matching the keyword anywhere
    # selected a GMP table covering nine IPOs, and the sample went out headed
    # "Allotment out — your IPO: check now" — correct behaviour on a message that names
    # no single company, and a poor demonstration of what a live alert looks like.
    "ipo_allotment": (agents().ipo, re.compile(
        r"(?is)(basis of allotment|allotment (status|date|out)).{0,80}?"
        r"[A-Z][A-Za-z&.\-]{3,}|[A-Z][A-Za-z&.\-]{3,}.{0,80}?"
        r"(basis of allotment|allotment (status|date|out))")),
    "ipo_listing": (agents().ipo, re.compile(
        r"(?is)(listing (today|date|gain)|listed at|lists on)")),
    "gmp_change": (agents().ipo, re.compile(
        r"(?is)(?!.*(?:\n.*){6}gmp)gmp.{0,60}?[0-9]")),   # one GMP, not a table of them
    "regulatory": (agents().regulatory, re.compile(
        r"(?i)\b(sebi|rbi|nse|bse|exchange)\b.{0,80}?"
        r"\b(circular|rule|norm|guideline|mandate|framework|approv|ban|penalt|amend|"
        r"deadline|effective|revis|introduc|allow|require)")),
    "trade_call": (agents().trade_call, re.compile(
        r"(?is)\b(buy|sell)\b.{0,60}\b(tgt|target|sl|stop\s*loss)\b")),
    "unclassified_escalation": (agents().escalation, re.compile(
        r"(?is)^(?=.{120,900}$)(?!.*(price band|join|premium)).*\S")),
}

def market_open(at: datetime | None = None) -> bool:
    d = (at or policy.now_ist()).astimezone(policy.IST)
    if d.weekday() >= 5:                       # Saturday, Sunday
        return False
    return MARKET_OPEN <= d.time() <= MARKET_CLOSE
