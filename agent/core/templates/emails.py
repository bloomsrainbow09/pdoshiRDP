"""Email assembly. Structure, budget and both renderers — never subject matter.

Split out of `agent/templates/render.py` in R5. The boundary was the hardest judgement
call in the restructure, so each helper's side is recorded in DECISIONS.md. The rule
applied: **does this decide HOW an email is built, or WHAT it says?**

Here, because they are mechanism:

  Email          the container, the word count, and the Flesch score measured on prose
                 only (a price table and a glossary are not writing; scoring them judges
                 formatting)
  MAX_WORDS      the 200-word and 60-character budgets — limits, not content
  MAX_SUBJECT
  TIER_PHRASE    how channel trust reads in English. Every vertical ranks its sources;
  _tier          "one of the more reliable channels you follow" says nothing about markets
  _channel       channel titles are keyword-stuffed in every niche, not just this one
  _subject       truncation on a word boundary
  _words         word counting
  _texts         which strings in a block list are reader-visible
  assemble       the budget fit, the glossary reservation, and pinning attribution to the
                 foot — all of it about ordering and space, none of it about content

NOT here, because they are subject matter: the money formatters, the market-check
rendering, the disclaimer, and the nine templates themselves. Those live in
`verticals/<name>/render.py`.

`assemble()` takes the glossary function and the footer as ARGUMENTS rather than
importing them. That inversion is the whole seam: the machinery that decides what fits
must not know which words need explaining, or a vertical with no jargon would still be
paying for a glossary lookup it does not need.
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_TPL = Path(__file__).resolve().parent
if str(_TPL) not in sys.path:
    sys.path.insert(0, str(_TPL))   # blocks.py and readability.py sit beside this file

import blocks as B              # noqa: E402
import readability              # noqa: E402


MAX_WORDS = 200

MAX_SUBJECT = 60


TIER_PHRASE = {
    "tier1": "one of the more reliable channels you follow",
    "tier2": "a middling one among the channels you follow",
    "tier3": "one of the weaker channels you follow",
    "newly-added": "a channel you added recently, not yet assessed",
    "useless": "a channel we rank as noise",
}


@dataclass
class Email:
    kind: str
    subject: str
    text: str
    html: str
    meta: dict = field(default_factory=dict)

    @property
    def words(self) -> int:
        return len(re.findall(r"[A-Za-z][A-Za-z'\-]*", self.text))

    @property
    def flesch(self) -> float:
        """Scored on the PROSE only.

        A price table is not prose and neither is a glossary; measuring them judges
        formatting rather than writing. `blocks.Rendered.prose` is the sentences.
        """
        return readability.flesch(self.meta.get("prose") or self.text)


# ── helpers ───────────────────────────────────────────────────────────────────

def _words(s: str) -> int:
    return len(re.findall(r"[A-Za-z][A-Za-z'\-]*", s))


def _channel(name) -> str:
    """A readable channel name.

    Channels stuff their titles with keywords: one here is called "Ipo and Stocks |
    IPO GMP | IPO Allotment | IPO | Pranav Constructions IPO Allotment |". Printed
    verbatim it drags four unexplained terms into an otherwise clean email.
    """
    n = str(name or "a channel").split("|")[0].split("(")[0].strip(" -–—™®©")
    n = re.sub(r"[^\w\s&.'-]+", "", n).strip()
    return n[:38] or "a channel"


def _tier(who: dict) -> str:
    return TIER_PHRASE.get((who or {}).get("tier"), "a channel you follow")


def _subject(s: str, limit: int = MAX_SUBJECT) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= limit:
        return s
    cut = s[:limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,-—:") + "…"


def _texts(bs: list) -> list:
    """Every reader-visible string in a block list — what the budget counts."""
    out = []
    for b in bs:
        if isinstance(b, B.Hero):
            out += [b.value, b.caption, b.sub]
        elif isinstance(b, (B.Para, B.Note)):
            out.append(b.text)
        elif isinstance(b, B.Steps):
            out += [b.title] + list(b.items)
        elif isinstance(b, B.Facts):
            out += [b.title] + [f"{l} {v}" for l, v in b.rows]
        elif isinstance(b, B.Quote):
            out += [b.text, b.source]
        elif isinstance(b, B.Source):
            out += [b.channel, b.tier_phrase, b.confirmed]
    return [x for x in out if x]


def _terms(terms_fn, text: str) -> list:
    """The vertical's glossary lookup, or nothing at all."""
    return list(terms_fn(text)) if terms_fn else []


def assemble(kind: str, kind_label: str, accent: str, subject: str,
             required: list, optional: list, footer: str,
             terms_fn=None, protect=(), meta: dict | None = None) -> Email:
    """Fit to the word budget, collect the terms used, and render both formats.

    `footer` and `terms_fn` are injected by the vertical. `terms_fn(text)` returns
    (term, explanation) pairs; a vertical whose readers already speak the language
    passes nothing and no glossary block is produced.

    Required blocks always ship. Optional blocks are supplied most-important-first and
    are added while the budget holds, so what gets cut is the least useful thing rather
    than whatever happened to be last.
    """
    chosen = list(required)
    used = sum(_words(t) for t in _texts(chosen))
    # The glossary block is added after fitting, so reserve room for it up front.
    # Without this the IPO email came out at 227 words against a 200-word limit.
    body_now = " ".join(_texts(chosen) + [t for b in optional for t in _texts([b])])
    reserve = sum(_words(f"{t} {m}") for t, m in _terms(terms_fn, body_now)[:6])
    budget = MAX_WORDS - _words(footer) - reserve
    for blk in optional:
        w = sum(_words(t) for t in _texts([blk]))
        if used + w > budget:
            continue
        chosen.append(blk)
        used += w

    # Attribution always sits at the FOOT, whatever order a template appended it in.
    # Putting it in `required` guaranteed it would ship (it used to get dropped by the
    # budget) but also floated it above the market check, so the email named its source
    # before it had said anything.
    src = [b for b in chosen if isinstance(b, B.Source)]
    if src:
        chosen = [b for b in chosen if not isinstance(b, B.Source)] + src[:1]

    # Terms are collected from what actually shipped, so the glossary never explains a
    # word that was cut and never misses one that stayed.
    body = " ".join(_texts(chosen))
    terms, seen = [], set()
    for shown, meaning in _terms(terms_fn, body):
        key = shown.lower()
        if key not in seen:
            seen.add(key)
            terms.append((shown, meaning))
    if terms:
        chosen.append(B.Words(terms))

    r = B.render_blocks(chosen, _subject(subject), kind_label, accent, footer)
    m = dict(meta or {})
    m.update({"protect": [x for x in protect if x], "prose": r.prose,
              "terms": [t for t, _ in terms]})
    return Email(kind, _subject(subject), r.text, r.html, m)
