"""Trade-call sub-agent.

These are DELIVERED. They are the user's own messages, from channels the user chose to
join, going to the user's own inbox; the system's job is to save them the scrolling and
attach the context Telegram does not show. It adds four things the raw message lacks:

  * structured fields, so a call can be read at a glance
  * who said it and how that channel has actually performed
  * whether a stop-loss was given — the strongest quality marker measured in this
    corpus, ranging from 80% of calls on `Trading bull stock` to near zero elsewhere
  * a standing note that these are third-party calls, not advice and not verified

Every call is also written to `content.trade_calls`, which is what makes per-channel
accuracy measurable later instead of merely asserted. Today `tiers.json` is scored on a
hand audit; this table is how that becomes continuous.
"""

import re
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

import asyncio                                       # noqa: E402
import market                                        # noqa: E402
from base import Outcome, ask, keep_grounded, trust  # noqa: E402
from engine import db as edb                         # noqa: E402

NAME = "trade_call"
NUMERIC = ("entry", "target", "target_2", "stop_loss", "cmp")

SYSTEM = """<role>
You extract the structure from a stock-market trade call posted on Telegram. You copy;
you do not judge whether the call is good.
</role>

<fields>
symbol        the stock, index or option written as given (e.g. "NIFTY", "TATAMOTORS",
              "BANKNIFTY 48000 CE"). null if none is named.
direction     "buy" | "sell" | null   (long=buy, short=sell)
instrument    "equity" | "futures" | "options" | "index" | null
entry         entry price or range, as written
target        first target, as written
target_2      second target if given
stop_loss     stop-loss, as written. null if the message gives none.
timeframe     "intraday" | "swing" | "positional" | "btst" | null
</fields>

<rules>
1. Copy numbers EXACTLY as written. Never compute a target or infer a stop-loss.
2. stop_loss is null unless the message actually states one. This field is measured —
   guessing it destroys the measurement.
3. "TGT", "T1", the dart emoji all mean target. "SL", "STOPLOSS", "SL-" mean stop_loss.
   "ABV"/"above" before a number is an entry trigger.
4. A bare price range with a target emoji and no verb is still a call: direction null,
   target set.
5. If this is a REVIEW of past calls ("5/6 hit", "target achieved") it is not a new
   call. Return {"symbol": null, "is_review": true}.
6. An EXIT level given against an entry is a stop-loss. "Buy near 212 / Exit 207 (close
   below on daily basis)" states a stop-loss of 207 — the word "SL" is not required.
7. A stop-loss can be a percentage or a point count, not only a price: "strict SL of 7%"
   and "SL 3 point" are both stop-losses. Copy them as written.
8. Mentioning a stop-loss without a level is NOT a stop-loss. "as long as trailing SL is
   respected" and "the market took my stoploss" both give no level: stop_loss is null.
</rules>

<output_format>
JSON only, no prose, no code fence.
</output_format>

<non_compliance>
Your likely failure is inventing a stop-loss because a call looks incomplete without
one. Its absence is the single most useful thing this extraction records. Leave it null.
</non_compliance>"""

_REVIEW = re.compile(r"(?i)\b(\d\s*/\s*\d|all\s+target|target\s+(achieved|hit|done)|"
                     r"done\s+of\s+the\s+day|booked|profit\s+booked)\b")

_DISCLAIMER = (
    "This is somebody else's call, forwarded from a Telegram channel you follow. It is "
    "not advice, nobody has verified it, and the channel is not accountable to you for "
    "it. You are seeing it because you asked to see these — decide for yourself.")


def _store(ev: dict, f: dict, who: dict) -> None:
    """Append to the accuracy ledger. Never blocks delivery — a bookkeeping failure
    must not cost the user their message."""
    try:
        edb.execute(
            """INSERT INTO content.trade_calls
                 (event_id, channel_id, channel_title, tier, posted_at, symbol, direction,
                  instrument, entry, target, target_2, stop_loss, has_stop_loss, timeframe,
                  raw_text)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (event_id) DO NOTHING""",
            (ev.get("id"), ev.get("channel_id"), who["title"][:120], who["tier"],
             ev.get("posted_at"), str(f.get("symbol") or "")[:60] or None,
             f.get("direction"), f.get("instrument"),
             str(f.get("entry") or "")[:40] or None,
             str(f.get("target") or "")[:40] or None,
             str(f.get("target_2") or "")[:40] or None,
             str(f.get("stop_loss") or "")[:40] or None,
             f.get("stop_loss") not in (None, "", "null"), f.get("timeframe"),
             (ev.get("text") or "")[:2000]))
    except Exception:
        pass


def sl_rate(channel_id, minimum: int = 10):
    """Share of this channel's stored calls that carried a stop-loss. None until there
    is enough history to say anything honest."""
    r = edb.fetch_one(
        """SELECT count(*) n, count(*) FILTER (WHERE has_stop_loss) sl
             FROM content.trade_calls WHERE channel_id = %s""", (channel_id,))
    if not r or r["n"] < minimum:
        return None
    return round(100.0 * r["sl"] / r["n"], 1)


async def handle(client, ev: dict, routed: dict) -> Outcome:
    text = (ev.get("text") or "").strip()
    who = trust(ev)

    data, _ = await ask(client, "analyst", SYSTEM, text[:2000], ev.get("id"))
    if not data:
        # Delivered anyway. A call we could not parse is still a call the user would
        # have read on their phone, and withholding it to protect a schema is backwards.
        return Outcome("notify", NAME, headline=f"Trade call from {who['title'][:40]}",
                       facts=[text[:400]], confidence=0.2,
                       caveats=[_DISCLAIMER,
                                "We could not read the structure of this call, so the "
                                "original text is shown as-is."],
                       reason="extraction failed; delivered raw rather than withheld")

    if data.get("is_review") or (not data.get("symbol") and _REVIEW.search(text)):
        return Outcome("discard", NAME,
                       reason="a review of past calls, not a new call — self-reported "
                              "performance is unauditable and the guard drops it")

    fields, dropped = keep_grounded(data, text, NUMERIC)
    _store(ev, fields, who)

    # Check the claim against the market rather than accepting the channel's framing.
    # A call posted "for educational purposes only" is still a claim about prices, and
    # prices are checkable. Runs in a thread because `market` is synchronous, and is
    # wrapped so that a network failure costs the reader a verification, never the
    # message itself.
    verified = {}
    try:
        verified = await asyncio.wait_for(
            asyncio.to_thread(market.check_call, fields, fields.get("symbol") or ""),
            timeout=30)
    except Exception:
        verified = {"checked": False, "why": "the market could not be reached"}

    has_sl = fields.get("stop_loss") not in (None, "")
    sym = fields.get("symbol") or "unnamed instrument"
    parts = []
    if fields.get("direction"):
        parts.append(str(fields["direction"]).upper())
    parts.append(str(sym))
    if fields.get("entry"):
        parts.append(f"at {fields['entry']}")
    if fields.get("target"):
        parts.append(f"to {fields['target']}")

    label = {"direction": "Action", "entry": "Entry", "target": "Target",
             "target_2": "Second target", "stop_loss": "Stop-loss",
             "timeframe": "Holding period", "instrument": "Instrument"}
    facts = [f"{label[k]}: {fields[k]}" for k in
             ("direction", "instrument", "entry", "target", "target_2", "stop_loss",
              "timeframe") if fields.get(k) not in (None, "")]
    facts.append(f"Source: {who['title'][:60]} ({who['tier'] or 'untiered'})")
    if who["note"]:
        facts.append(f"What we know about this channel: {who['note'][:220]}")

    caveats = [_DISCLAIMER]
    if not has_sl:
        caveats.append(
            "No stop-loss was given. A stop-loss is the price at which you accept the "
            "trade was wrong and exit. Without one there is no stated limit on the "
            "loss — this is the single biggest quality difference between channels.")
    rate = sl_rate(ev.get("channel_id"))
    if rate is not None:
        caveats.append(f"This channel has included a stop-loss on {rate}% of the calls "
                       f"we have recorded from it.")
    if (who["weight"] or 0) < 0.5:
        caveats.append("This is a lower-tier channel in your folder — its calls have not "
                       "held up as well as tier-1 sources.")
    if dropped:
        caveats.append(f"Figures not found in the original message were dropped: "
                       f"{', '.join(dropped)}.")

    fields["_market"] = verified

    return Outcome(
        action="notify", agent=NAME, headline="Trade call: " + " ".join(parts),
        fields=fields, facts=facts, caveats=caveats,
        confidence=round(min(1.0, (who["weight"] or 0.3) + (0.2 if has_sl else 0)), 2),
        reason=(f"trade_call {sym}: stop_loss={'yes' if has_sl else 'NO'}, "
                f"source {who['tier']} — delivered with context"))
