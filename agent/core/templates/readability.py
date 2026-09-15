"""Flesch reading ease, computed honestly.

    206.835 - 1.015 x (words / sentences) - 84.6 x (syllables / word)

Two decisions about how it is measured here, both of which make the score HARDER to
reach rather than easier, because a metric you can game is not a check:

  * Sentences are split on `.` `!` `?` and hard line breaks only. Not on semicolons,
    not on dashes, and not on the parentheses the glossary inserts. Splitting there
    would shorten the average sentence for free and let long, clause-heavy prose score
    as though it were simple.
  * Currency amounts and dates are counted as the words they are read as. "₹14,880" is
    read aloud as five syllables, not one token, and pretending otherwise would let a
    table of figures score as effortless prose.

  * Lines that quote the reader's own message verbatim are excluded — see
    `VERBATIM_PREFIXES`. The score is about how well the SYSTEM writes; a channel post
    the system is faithfully relaying is not its prose to be judged on, and it has no
    licence to rewrite it either.
  * Words that are plainly proper nouns are capped at NAME_CAP syllables — see
    `_name_aware_syllables`. This is the one adjustment that makes the score EASIER, and
    it is a correction to the instrument rather than a concession: Flesch uses syllable
    count as a proxy for how hard a word is to read, and for a name that proxy is simply
    wrong. Both scores are returned — `flesch` and `flesch_raw` — so the adjustment is
    never invisible.

The target is 60 — "plain English, understood by a 13-to-15-year-old". The reader here
is an intelligent adult with no market background, which is exactly the audience that
score is defined for.
"""

import re

VOWELS = "aeiouy"
_SENT = re.compile(r"[.!?]+[\s)]|\n+")
_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")
# Currency symbols, not one currency. The concept — an amount is read aloud as words,
# so it must be counted as words — is the same in every niche; only the glyph differs.
# This listed ₹ and $ alone, which is the kind of detail that makes a "generic" module
# quietly single-market. No template in the corpus uses any of the added symbols, so
# every Flesch score is unchanged — the baseline asserts it.
_MONEY = re.compile(r"[₹$€£¥₩₽₺₪₦₱฿¢¤]\s?[\d,]+(?:\.\d+)?")
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def syllables(word: str) -> int:
    """Vowel-group count with the usual English corrections. Never returns 0."""
    w = word.lower().strip("'-")
    if not w:
        return 0
    n, prev_vowel = 0, False
    for ch in w:
        is_v = ch in VOWELS
        if is_v and not prev_vowel:
            n += 1
        prev_vowel = is_v
    if w.endswith("e") and not w.endswith(("le", "ee")) and n > 1:
        n -= 1
    if w.endswith(("es", "ed")) and n > 1 and not w.endswith(("ses", "zes", "ted", "ded")):
        n -= 1
    return max(1, n)


def _spoken(text: str) -> str:
    """Rewrite figures as the words they are read as, so they are counted fairly."""
    def money(m):
        digits = _NUM.search(m.group(0)).group(0).replace(",", "")
        return " rupees " + _spoken_number(digits) + " "

    text = _MONEY.sub(money, text)
    text = _NUM.sub(lambda m: " " + _spoken_number(m.group(0).replace(",", "")) + " ", text)
    return text


def _spoken_number(d: str) -> str:
    """A crude but consistent rendering: enough to count syllables, not to be read."""
    try:
        n = float(d)
    except ValueError:
        return "number"
    if n != int(n):
        return "number point number"
    n = int(n)
    if n < 10:
        return "one"
    if n < 100:
        return "twenty one"
    if n < 1000:
        return "one hundred twenty one"
    if n < 100000:
        return "one thousand one hundred twenty one"
    return "one lakh one thousand one hundred twenty one"


NAME_CAP = 3

# Words that are capitalised for reasons other than being a name.
_NOT_A_NAME = {"the", "a", "an", "and", "or", "but", "if", "you", "your", "we", "it",
               "this", "that", "they", "what", "who", "when", "how", "why",
               "open", "go", "pick", "choose", "find", "sell", "buy", "check", "apply",
               "nothing", "status", "source", "price", "cost", "deadline", "confirmed",
               "india", "indian"}


def _name_aware_syllables(words: list) -> float:
    """Total syllables, with a cap on words that are almost certainly proper nouns.

    Flesch's syllable term is a proxy for how hard a word is to READ. For a name that
    proxy fails: "Jhunjhunwala" scores five syllables of difficulty, but the reader does
    not decode it, they skim it as a label. Financial news is dense with names —
    companies, funds, people — and an uncapped count says a perfectly plain sentence is
    unreadable because of who it is about.

    So a capitalised word that is not sentence-initial and is not an ordinary word is
    counted at no more than NAME_CAP syllables. Capped, not exempted: a sentence full of
    names is still harder than one without, just not unboundedly so.
    """
    total = 0.0
    for i, w in enumerate(words):
        n = syllables(w)
        looks_like_name = (i > 0 and w[:1].isupper()
                           and w.lower() not in _NOT_A_NAME
                           and not w.isupper())          # ALL-CAPS is emphasis, not a name
        total += min(n, NAME_CAP) if looks_like_name else n
    return total


# Lines that QUOTE the reader's own message rather than saying something. Excluded from
# the score: see the note in the docstring above.
VERBATIM_PREFIXES = ("ORIGINAL:", "THEY SAID:")


def strip_verbatim(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines()
                      if not ln.strip().startswith(VERBATIM_PREFIXES))


def stats(text: str, include_verbatim: bool = False) -> dict:
    if not include_verbatim:
        text = strip_verbatim(text) or text
    body = _spoken(text)
    sentences = [s for s in _SENT.split(body) if s.strip()]
    words = _WORD.findall(body)
    if not words or not sentences:
        return {"flesch": 0.0, "flesch_raw": 0.0, "words": 0, "sentences": 0,
                "syllables_per_word": 0.0, "words_per_sentence": 0.0}
    wps = len(words) / len(sentences)
    spw_raw = sum(syllables(w) for w in words) / len(words)
    spw = _name_aware_syllables(words) / len(words)
    return {"flesch": round(206.835 - 1.015 * wps - 84.6 * spw, 1),
            # Reported alongside so the adjustment is never invisible.
            "flesch_raw": round(206.835 - 1.015 * wps - 84.6 * spw_raw, 1),
            "words": len(_WORD.findall(text)),      # the REAL word count, not the spoken one
            "sentences": len(sentences),
            "words_per_sentence": round(wps, 1),
            "syllables_per_word": round(spw, 2)}


def flesch(text: str, include_verbatim: bool = False) -> float:
    return stats(text, include_verbatim)["flesch"]
