"""The material-signal veto for the TRADING vertical.

The code-level answer to a question no model gets to overrule: **does this message carry
something a reader could act on?** A price level, a target, a stop-loss, a circuit, an
option strike, a price band.

It exists because the guard was killing real trade calls — 6 of 10 material messages in
the P10 measurement (D-14). One regex, computed once per event, consulted at all three
doors that can discard a message: the guard, a confident router calling it a fragment,
and the importance assessor. Three doors, one rule.

**This is vertical knowledge by definition.** `core` cannot know what "worth keeping"
looks like in a niche it has never seen, which is why the contract exposes it as
`has_material_signal()` and a vertical with no such concept simply returns False.

Moving it here is also what breaks the last import cycle: the plugin used to reach into
`agent/orchestrator` for this one function, so the orchestrator could not reach the
plugin. Now the dependency runs one way.

Two corpus-driven details, both of which were bugs first:
  * `re.S` — channels pad with blank lines. "Bank Nifty




45388 To 45441" is one
    message, and without DOTALL the gap defeats every pattern that spans characters.
  * bare ranges require the word "to" or an arrow, never a lone hyphen: "12-2026" is a
    date, and matching it vetoed the guard on ordinary chatter.
"""

import re

# A message the guard is NOT allowed to drop, whatever it thinks.
#
# The evaluation found the guard killing real content before the router ever saw it:
# "BUY TORANTPHARM 4850 CE ABOVE...", "ZOMATO HIT UPPER CIRCUIT", "NIFTY CMP : 24 EXIT
# At CMP". Six of the ten missed material messages died here, which made the guard the
# single largest contributor to the one metric that must not fail.
#
# A prompt rule was already in place and was not enough — "when unsure, return
# drop=false" loses to a model that is confident and wrong. So the veto is in code. It
# is deliberately narrow: a ticker-shaped word next to a number, an explicit direction,
# a stated level, or an exchange event. Cheap to evaluate, and it costs at most one
# extra router call on a message that turns out to be noise.
_SIGNAL = re.compile(
    r"(?ix)"
    r"  \b(buy|sell|short|long|exit|book|add|accumulate|hold)\b .{0,40} \d"
    r"| \b(tgt|target|sl|stop\s*loss|stoploss|cmp|ltp|abv|above|below)\b .{0,20} \d"
    r"| \b(upper|lower)\s+circuit\b"
    r"| \b\d{3,5}\s*(ce|pe)\b"
    r"| \b(bank\s*nifty|fin\s*nifty|nifty|sensex)\b .{0,30} \d"
    # Channels write the rupee sign on either side: "₹1060 to ₹1125" and "1060 ₹ to
    # 1125 ₹ 🎯" are the same call, and only the second form appears in the corpus row
    # that exposed this.
    r"| (₹\s*\d[\d,.]* | \d[\d,.]*\s*₹) .{0,25} (to|-|–|→|🎯)"
    r"| \b(price\s*band|lot\s*size|allot|gmp|listing)\b"
    # A bare price range, no currency symbol: "100 TO 115 ++". Two numbers of
    # 2-6 digits joined by to/-/arrow is a price range in this corpus; dates are
    # written with month names or slashes and do not match.
    # "to" or an arrow only, never a bare hyphen: "12-2026" is a date and matched,
    # which would have vetoed the guard on ordinary chatter. Every real example in the
    # corpus writes the word — "100 TO 115 ++".
    r"| (?<![\d/.])\d{2,6}\s*(?:to|→|–>)\s*\d{2,6}(?![\d/.])"
    # A ticker and a chart event, with no number at all: "COALINDIA trendline
    # BREAKOUT". The ticker alone is not a signal; the pairing is.
    r"| (?<![A-Za-z])[A-Z]{4,}(?![a-z]) .{0,40} (breakout|break\s*out|breakdown|reversal|resistance|support|trendline)",
    # DOTALL. Channels pad with blank lines — "Bank Nifty\n\n\n\n\n45388 To 45441" is
    # one message, and without this the gap between the name and the number defeats
    # every pattern above that spans characters.
    re.S)


def has_trading_signal(text: str) -> bool:
    """True when a message carries something a trader could act on."""
    return bool(_SIGNAL.search(text or ""))


def has_material_signal(text: str) -> bool:
    """The contract name. `has_trading_signal` is what the orchestrator called it."""
    return has_trading_signal(text)
