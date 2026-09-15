"""Jargon control.

Two jobs, and the second is the one that matters:

  `explain()`     expands a term the first time it appears and never again — repeating
                  the gloss in every paragraph is its own kind of unreadable.
  `unexplained()` is the ENFORCEMENT side. It scans finished text for any blocklisted
                  term that appears without its explanation nearby, and gate P08 fails
                  the build on a single hit.

The blocklist was not invented. Every term in it appears in the archive, and each one is
a word the reader is assumed not to know — the brief is explicit that they do not know
what GMP, lot size, ASBA, allotment, mainboard/SME or cut-off price mean.

Glosses are written to be read at a glance on a phone lock screen: no term is explained
using another term from this list, which is checked by `self_check()` and asserted by
the gate. An explanation that needs its own explanation has not explained anything.
"""

import re

# term (as matched, case-insensitive) -> the parenthetical that must accompany its first
# use. Keep each under ~14 words: these are read on a lock screen.
TERMS = {
    "gmp": "grey-market premium — an unofficial guess at the price it will first trade at",
    "grey market premium":
        "an unofficial guess at the price it will first trade at",
    "lot size": "the fixed number of shares you must buy at once — you cannot buy fewer",
    "lot": "the fixed bundle of shares you must buy at once",
    "asba": "the system that blocks the money in your bank instead of taking it",
    "allotment": "finding out whether you actually got any shares",
    "mainboard": "a larger, more established company selling shares to the public",
    "sme": "small and medium enterprise — a smaller, riskier company doing the same",
    "cut-off price": "ticking this lets the app pay the final price, whatever it lands on",
    "cutoff price": "ticking this lets the app pay the final price, whatever it lands on",
    "price band": "the range the share will be priced in",
    "issue size": "how much money the company is raising in total",
    "face value": "an accounting number printed on the share; it does not affect what you pay",
    "ofs": "existing owners cashing out, so this money does not reach the company",
    "offer for sale":
        "existing owners cashing out, so this money does not reach the company",
    "fresh issue": "new shares, so this money does go to the company",
    "oversubscribed": "more people applied than there are shares, so not everyone gets any",
    "subscription": "how many people have applied so far",
    "listing": "the day the share starts trading and you can sell it",
    "demat": "the account that holds your shares, like a bank account for shares",
    "upi mandate": "the payment approval your bank app asks you to accept",
    "stop-loss": "the price at which they would give up and sell to limit the loss",
    "stop loss": "the price at which they would give up and sell to limit the loss",
    "target": "the price they expect it to reach",
    "intraday": "bought and sold on the same day",
    "swing": "held for a few days to a few weeks",
    "positional": "held for weeks or months",
    "btst": "buy today, sell tomorrow",
    "futures": "a contract to trade later at a set price — riskier than buying the share",
    "options": "a contract that can expire worthless — much riskier than buying the share",
    "ce": "call option — a bet the price rises, which expires worthless if it does not",
    "pe": "put option — a bet the price falls, which expires worthless if it does not",
    "circuit": "a trading halt the exchange imposes when a price moves too far too fast",
    "qib": "large institutional buyers such as banks and funds",
    "hni": "high-net-worth individual — someone applying with a much larger amount",
    "nii": "non-institutional investor — a larger applicant than a retail one",
    "retail quota": "the share of the offer reserved for small investors like you",
    "rhp": "the official prospectus filed before the offer opens",
    "drhp": "the draft prospectus, filed earlier than the final one",
    "sebi": "the body that writes India's market rules",
    "regulator": "the body that writes and enforces the market's rules",
    "nse": "the National Stock Exchange, one of India's two main share markets",
    "bse": "the Bombay Stock Exchange, the other main share market",
    "rbi": "India's central bank",
    "t+1": "the shares and money change hands one working day after the trade",
    "t+0": "the shares and money change hands on the same day as the trade",
    "margin": "money your broker requires you to keep aside to hold a position",
    "fii": "foreign investors",
    "dii": "Indian institutions such as mutual funds and insurers",
    "pat": "profit after tax — what the company actually kept",
    "cmp": "the price it is trading at right now",
    # Added after the P8 jury flagged each of these on real rendered emails. Every one
    # reached a reader unexplained before it was added, which is the only evidence that
    # matters for a blocklist.
    "ipo": "selling shares to the public for the first time",
    "holdings": "the list of shares you own, in your app",
    "market price": "whatever the share is trading at right now",
    "allotted": "given shares",
    "stake": "a part-ownership of a company",
    "block deal": "one very large trade agreed between two big investors",
    "q1": "the first three months of a company's reporting year",
    "q2": "the second three months of a company's reporting year",
    "q3": "the third three months of a company's reporting year",
    "q4": "the last three months of a company's reporting year",
    "quarter": "a three-month reporting period",
    "dvr": "a share class with fewer voting rights",
    "promoter": "the family or group that founded and controls the company",
    "buyback": "the company buying its own shares back from investors",
    "bonus": "free extra shares given to people who already hold them",
    "dividend": "a cash payout to shareholders",
    "fpo": "a further sale of shares by a company already on the market",
    "anchor": "large investors who buy in a day before everyone else",
    "delisting": "the share being removed from the exchange",
    "portfolio": "everything you own, taken together",
    "holding": "shares you own",
    "shareholding": "the slice of a company somebody owns",
    "joint venture": "a business two companies run together",
    "percentage points": "the plain difference between two percentages",
    "financial year": "the April-to-March year Indian companies report on",
    "trading restrictions": "limits the exchange puts on how a share can be traded",
    "surveillance": "extra monitoring the exchange puts on a share",
    "asm": "a watchlist the exchange puts risky shares on, with extra limits",
    "settlement": "the day the shares and the money actually change hands",
    "jv": "a business two companies run together",
    "crore": "ten million rupees",
    "cr": "crore, meaning ten million rupees",
    "lakh": "one hundred thousand rupees",
    "derivatives": "contracts whose value follows a share or index, rather than "
                   "the share itself",
}

# Terms that must never reach the reader unexplained. Everything above with a gloss.
BLOCKLIST = tuple(sorted((t for t, g in TERMS.items() if g), key=len, reverse=True))


def _word_re(term: str) -> re.Pattern:
    """Match the term as a whole word, tolerating the punctuation channels use."""
    esc = re.escape(term).replace(r"\-", r"[-\s]?").replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![A-Za-z0-9]){esc}(?![A-Za-z0-9])", re.IGNORECASE)


_COMPILED = {t: _word_re(t) for t in TERMS if TERMS[t]}


def overlapping(term: str) -> set:
    """Terms whose meaning this one already covers, in either direction.

    Explaining "lot" also settles "lot size" for the reader, and vice versa. Without
    this the same idea gets a parenthetical twice in one email, which reads worse than
    not explaining it at all.
    """
    return {t for t in BLOCKLIST if t == term or t in term or term in t}


def explain(text: str, already: set | None = None, protect=()) -> str:
    """Gloss the first use of each term, scanning LEFT TO RIGHT.

    Order matters and position beats length. Scanning longest-first would explain
    "lot size" in a later line while leaving the earlier bare "lot" unexplained — the
    reader meets the word before the explanation. So the scan walks the text in order
    and, at each position, glosses the longest term that starts there.
    """
    seen = already if already is not None else set()

    # Spans that must be left alone. A channel called "Ipo and Stocks" is a NAME, and
    # glossing inside it produced "Ipo (selling shares to the public for the first time)
    # and Stocks says:" — which is worse than the jargon it was trying to remove.
    guard = []
    for frag in protect:
        f = str(frag or "").strip()
        j = text.find(f) if len(f) >= 3 else -1
        while j >= 0:
            guard.append((j, j + len(f)))
            j = text.find(f, j + len(f))

    out, i = [], 0
    while i < len(text):
        hit = None
        if not any(a <= i < b for a, b in guard):
            for term in BLOCKLIST:                  # longest first AT THIS POSITION
                if term in seen:
                    continue
                m = _COMPILED[term].match(text, i)
                if m and not any(a < m.end() and m.start() < b for a, b in guard):
                    hit = (term, m)
                    break
        if hit:
            term, m = hit
            out.append(text[i:m.end()])
            tail = text[m.end():m.end() + 2].strip()
            if not tail.startswith("("):            # writer has not glossed it already
                out.append(f" ({TERMS[term]})")
            seen |= overlapping(term)
            i = m.end()
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def unexplained(text: str, protect=()) -> list:
    """Blocklisted terms the reader meets with no explanation anywhere in the email.

    A term counts as covered if it, or any term overlapping it, carries a gloss — the
    reader who has been told what a lot is does not also need "lot size" defined.

    `protect` names spans that are proper nouns. A channel in this folder is called
    "Ipo and Stocks", and the word inside a NAME is not a term the email is using: it
    cannot be glossed there without producing "Ipo (selling shares to the public for the
    first time) and Stocks". The templates that print a name pass it here, and they also
    say the words "a Telegram channel called" in front of it, so the reader can see it
    is a name rather than vocabulary they are expected to know.
    """
    guard = []
    for frag in protect:
        f = str(frag or "").strip()
        j = text.find(f) if len(f) >= 3 else -1
        while j >= 0:
            guard.append((j, j + len(f)))
            j = text.find(f, j + len(f))

    def free(m):
        return not any(a < m.end() and m.start() < b for a, b in guard)

    glossed, present = set(), set()
    for term in BLOCKLIST:
        for m in _COMPILED[term].finditer(text):
            if not free(m):
                continue
            present.add(term)
            if text[m.end():m.end() + 2].strip().startswith("("):
                glossed |= overlapping(term)
                break
    return [t for t in BLOCKLIST if t in present and t not in glossed]


def self_check() -> list:
    """No gloss may itself contain a blocklisted term. An explanation that needs an
    explanation has not explained anything — asserted by gate P08."""
    bad = []
    for term, gloss in TERMS.items():
        if not gloss:
            continue
        for other in BLOCKLIST:
            if other == term or other in term or term in other:
                continue
            if _COMPILED[other].search(gloss):
                bad.append((term, other))
    return bad


def terms_in(text: str, already=()) -> list:
    """Terms the text uses, as (term, meaning), longest-match-first, no duplicates.

    The collect-and-footnote alternative to `explain()`. Inserting a parenthetical mid-
    sentence turned every line into a run-on:

        Choose 1 lot (the fixed bundle of shares you must buy at once) at the cut-off
        price (ticking this lets the app pay the final price, whatever it lands on).

    Marking the term where it appears and explaining it once at the end keeps the
    sentence a sentence, and keeps the guarantee that nothing reaches the reader
    unexplained. Overlapping terms collapse to one entry: a reader told what a lot is
    does not also need "lot size" defined.
    """
    seen, out = set(already), []
    i = 0
    while i < len(text):
        hit = None
        for term in BLOCKLIST:                      # longest first at this position
            if term in seen:
                continue
            m = _COMPILED[term].match(text, i)
            if m:
                hit = (term, m)
                break
        if hit:
            term, m = hit
            out.append((text[m.start():m.end()], TERMS[term]))
            seen |= overlapping(term)
            i = m.end()
        else:
            i += 1
    return out


def unexplained_given(text: str, defined: list, protect=()) -> list:
    """Blocklisted terms in `text` that the `defined` list does not cover.

    The footnote equivalent of `unexplained()`: a term counts as covered when it, or a
    term overlapping it, appears in the glossary block at the end of the email.
    """
    covered: set = set()
    for shown, _meaning in defined:
        low = str(shown).lower().strip()
        for term in BLOCKLIST:
            if term == low or term in low or low in term:
                covered |= overlapping(term)

    guard = []
    for frag in protect:
        f = str(frag or "").strip()
        j = text.find(f) if len(f) >= 3 else -1
        while j >= 0:
            guard.append((j, j + len(f)))
            j = text.find(f, j + len(f))

    bad = []
    for term in BLOCKLIST:
        if term in covered:
            continue
        for m in _COMPILED[term].finditer(text):
            if any(a < m.end() and m.start() < b for a, b in guard):
                continue
            bad.append(term)
            break
    return bad
