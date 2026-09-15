"""When to send, when to hold, and when to stop sending.

Three rules, each with a measured reason:

**Dedupe.** 49% of texted posts in the archive are duplicates — the same IPO is
announced by six channels within minutes. Six identical emails is worse than none,
because it teaches the reader to filter the sender.

**Rate limit.** Six emails an hour by default. Past that the overflow is batched rather
than dropped: nobody reads forty emails, and a Gmail account that sends forty an hour
gets flagged.

**Quiet hours.** 22:00–07:00 IST, holding everything except the two intents the taxonomy
marks `quiet_hours_override`: `ipo_allotment` and `regulatory`. Both are time-critical —
an allotment window closes, a rule takes effect on a date. Everything else waits for
07:00 and arrives as one digest.

The exceptions come from `taxonomy.json`, not from a list here, so a category cannot be
made non-urgent in one file and urgent in another.
"""

import json
import re
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT))

from engine import config as econf  # noqa: E402
from engine import db as edb        # noqa: E402

sys.path.insert(0, str(ROOT))
from core import active             # noqa: E402

TAXONOMY = active.taxonomy()
INTENTS = TAXONOMY["intents"]

IST = timezone(timedelta(hours=5, minutes=30))
QUIET_START = time(22, 0)
QUIET_END = time(7, 0)
DEFAULT_RATE = 6                      # emails per hour before batching kicks in

# Intents that may wake the user. Read from the taxonomy so there is one definition.
URGENT = {k for k, v in INTENTS.items() if v.get("quiet_hours_override")}


def rate_limit() -> int:
    try:
        return max(1, int(econf.get("ALERT_RATE_PER_HOUR", DEFAULT_RATE)))
    except (TypeError, ValueError):
        return DEFAULT_RATE


def now_ist() -> datetime:
    return datetime.now(IST)


def in_quiet_hours(at: datetime | None = None) -> bool:
    t = (at or now_ist()).astimezone(IST).time()
    return t >= QUIET_START or t < QUIET_END       # the window crosses midnight


def next_open(at: datetime | None = None) -> datetime:
    """07:00 IST — the next moment a held message may go out."""
    d = (at or now_ist()).astimezone(IST)
    out = d.replace(hour=QUIET_END.hour, minute=0, second=0, microsecond=0)
    if d.time() >= QUIET_END:
        out += timedelta(days=1)
    return out


# ----------------------------------------------------------------- dedupe ----

_TRIM = re.compile(r"[^a-z0-9 ]+")
_SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Strip everything that varies between channels reposting the same fact: emoji,
    punctuation, promo tails, casing, whitespace."""
    t = _TRIM.sub(" ", (text or "").lower())
    t = _SPACE.sub(" ", t).strip()
    return t


# Which words identify nothing is niche vocabulary — "allotment", "gmp", "mainboard"
# and "nse" mean nothing in a recipe vertical, and its filler would mean nothing here.
# So the stop-list moved to verticals/<name>/boilerplate.py in R6 and the SCORER, with
# its calibrated 0.50 threshold, stayed. Loaded by name; a vertical without one simply
# has no words to ignore.
def _boilerplate() -> set:
    try:
        return set(active.module("boilerplate").WORDS)
    except FileNotFoundError:
        return set()


_BOILERPLATE = _boilerplate()

_NUMS = re.compile(r"\d[\d,]*\.?\d*")
_WORDS = re.compile(r"[A-Za-z][A-Za-z&.\-]{2,}")

# Measured, not chosen. agent/bench/calibrate_dedupe.py scores 127 real repost pairs
# (the same company's IPO posted by two different channels, mined from the archive)
# against 3000 different-company pairs:
#
#   threshold   recall   false positives
#     0.41       0.480    0        <- the exact zero-FP point, and neg_max is 0.400
#     0.50       0.449    0        <- chosen: 3 points of recall for real margin
#     0.35       0.528    5
#
# 0.50 sits well clear of the worst negative. The asymmetry decides it: a false positive
# SUPPRESSES an alert the reader never learns existed, while a missed near-duplicate
# costs them one extra email.
SEMANTIC_THRESHOLD = 0.50


def _signature(text: str) -> tuple:
    """(entity words, figures) — what identifies the FACT, with boilerplate removed."""
    words = {w.lower().strip(".&-") for w in _WORDS.findall(text or "")}
    words = {w for w in words if w not in _BOILERPLATE and len(w) > 3}
    nums = {n.replace(",", "").rstrip(".").lstrip("0") or "0"
            for n in _NUMS.findall(text or "")}
    return words, {n for n in nums if len(n) >= 2}   # single digits identify nothing


def _jac(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def similarity(a: str, b: str) -> float:
    """How likely two alerts are about the same fact.

    NOT free-text similarity. This runs on RENDERED EMAILS, and two emails from one
    template share every line of boilerplate — the same TO APPLY steps, the same
    disclaimer. Comparing that text measures the template, not the fact, and would rate
    two completely different IPOs as near-identical. Word 4-grams over the raw text
    scored genuine reposts at 0.167 while different-company pairs reached 0.127: no
    threshold separates those.

    So the comparison is a fact signature — the entity words and the figures, with the
    shared vocabulary stripped out. Both halves are needed and neither is enough on its
    own: two IPOs opening the same week share dates and lot sizes but not names, and one
    company named in two unrelated posts shares the name but not the figures.
    """
    wa, na = _signature(a)
    wb, nb = _signature(b)
    return round(0.5 * _jac(wa, wb) + 0.5 * _jac(na, nb), 4)


def _dedupe_extra(intent: str, fields: dict) -> str:
    """The active vertical's extra discriminator, or nothing."""
    try:
        from core import registry
        return registry.load(active.name()).dedupe_extra(intent, fields) or ""
    except Exception:
        # A vertical that cannot be loaded must not take delivery down with it: the key
        # degrades to intent+subject+day, which over-collapses rather than duplicating.
        return ""


def dedupe_key(ev: dict, outcome: dict) -> str:
    """The identity of the FACT, not of the message.

    Six channels announcing one IPO must collapse to one key, so the channel and the
    message id must not appear in it. What identifies the fact is the intent, the entity
    it is about, and the day.
    """
    f = outcome.get("fields") or {}
    intent = ev.get("intent") or outcome.get("agent") or "unknown"
    subject = (f.get("company") or f.get("symbol") or f.get("regulator") or "").strip()
    if subject:
        subject = normalise(str(subject))[:60]
    else:
        # Nothing was extracted. Keying on the outcome HEADLINE puts a generic failure
        # string in the key - "ipo like message no company named" - so two unrelated
        # unreadable messages get two keys and the reader gets two identical emails.
        # Key on the message's own content instead: different messages, different keys;
        # the same message reposted by six channels, one key.
        import hashlib
        body = normalise(ev.get("text") or "")[:400]
        subject = "nokey-" + hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]
    day = (ev.get("posted_at") or datetime.now(IST)).strftime("%Y-%m-%d") \
        if hasattr(ev.get("posted_at") or datetime.now(IST), "strftime") else "undated"
    # intent + subject + day is universal. What ELSE makes two of them different facts
    # is niche knowledge, so the vertical supplies it. R6 moved two branches out of
    # here: a grey-market premium that moved, and a call at different levels.
    extra = _dedupe_extra(intent, f)
    return f"{intent}:{subject}:{day}{extra}"


def semantic_duplicate(text: str, hours: int = 24,
                       threshold: float = SEMANTIC_THRESHOLD) -> dict | None:
    """A near-identical alert already queued or sent recently.

    The dedupe key catches reposts of the same FACT. This catches the same fact stated
    differently enough to key differently — a forwarded copy with the company name
    spelled another way. It compares against what was actually queued, not the raw
    messages, because that is what the reader would see twice.
    """
    rows = edb.fetch_all(
        """SELECT id, dedupe_key, subject, status
             FROM content.notifications
            WHERE created_at > now() - (%s || ' hours')::interval
            ORDER BY id DESC LIMIT 200""", (hours,))
    best, score = None, 0.0
    for r in rows:
        # The dedupe_key already encodes intent + entity + day + the distinguishing
        # figures, so it IS a fact signature. Comparing against it rather than the body
        # keeps template boilerplate out of the comparison on both sides.
        s = similarity(text, f"{r['subject']} {r['dedupe_key'].replace(':', ' ')}")
        if s > score:
            best, score = r, s
    if best and score >= threshold:
        return {"id": best["id"], "dedupe_key": best["dedupe_key"],
                "similarity": round(score, 3), "status": best["status"]}
    return None


# ------------------------------------------------------------- send window ----

def sent_last_hour() -> int:
    return edb.fetch_one(
        "SELECT count(*) n FROM content.notifications "
        "WHERE status = 'sent' AND sent_at > now() - interval '1 hour'")["n"]


def verdict(intent: str, at: datetime | None = None) -> dict:
    """May this go out right now?

    Returns one of:
      send    deliver immediately
      hold    quiet hours; deliver in the 07:00 digest
      batch   over the hourly limit; roll into the next digest
    """
    at = at or now_ist()
    urgent = intent in URGENT
    if in_quiet_hours(at) and not urgent:
        return {"action": "hold", "why": "quiet hours (22:00-07:00 IST)",
                "until": next_open(at)}
    used = sent_last_hour()
    limit = rate_limit()
    if used >= limit and not urgent:
        return {"action": "batch",
                "why": f"hourly limit reached ({used}/{limit}) — batched, not dropped",
                "until": at + timedelta(hours=1)}
    if urgent and (in_quiet_hours(at) or used >= limit):
        return {"action": "send",
                "why": f"{intent} overrides quiet hours and the rate limit — "
                       f"the taxonomy marks it time-critical"}
    return {"action": "send", "why": f"within limits ({used}/{limit} this hour)"}
