"""The nine email templates for the TRADING vertical.

Moved out of `agent/templates/render.py` in R5. What stayed behind is mechanism —
`core.templates.emails` owns the word budget, the subject truncation, the block-to-text
walk and the assembly that pins attribution to the foot. What is here is subject matter:
rupees, price bands, lot sizes, stop-losses, the market check, and the nine emails
themselves.

**Deterministic, not model-written.** The brief sets four hard constraints — under 200
words, subject under 60 characters, plain English, no unexplained jargon. A template can
*guarantee* all four; a model can only be asked for them, at 3 a.m., with nobody reading
the output. The sentences here are assembled from fields a sub-agent extracted from the
message, so nothing in a delivered email is prose a model invented at send time.

**Written as sentences, not as labelled fragments.** The previous version produced
"YOU FIND OUT: 22 Sep." — a machine filling slots, with the currency symbol missing and
inline glosses turning every instruction into a run-on. That happened because the text
was tuned to score well on a readability metric: short choppy clauses score high and read
like a telegram. So prose is prose, numbers live in a table where a short label is
genuinely the right way to write, and terms are explained once at the end.

**Attribution over editorial.** The system never recommends anything in its own voice.
When a channel makes a call the email says so, names the channel, and says how that
channel has actually performed. The reader is entitled to their own messages; what is
added is context, never a veto and never an opinion.
"""

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _p in (str(_ROOT), str(_HERE), str(_ROOT / "core" / "templates")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import blocks as B                                   # noqa: E402  core block model
import glossary                                      # noqa: E402  this folder
from core.templates.emails import (                  # noqa: E402
    Email, MAX_SUBJECT, MAX_WORDS, TIER_PHRASE,
    _channel, _subject, _texts, _tier, _words, assemble,
)


def _assemble(kind, kind_label, accent, subject, required, optional,
              footer=None, protect=(), meta=None) -> Email:
    """This vertical's assembly: core's machinery, plus our disclaimer and glossary.

    Kept under the original private name and signature so the nine templates below are
    unchanged from before the split — which is what makes their output byte-identical.
    """
    return assemble(kind, kind_label, accent, subject, required, optional,
                    footer=DISCLAIMER if footer is None else footer,
                    terms_fn=glossary.terms_in, protect=protect, meta=meta)


DISCLAIMER = ("You could lose money. Nobody can tell you what this will be worth later. "
              "This email reports what a channel said; it is not advice.")


def _rupees(v) -> str:
    """A money amount the reader can check against their bank app."""
    if v in (None, ""):
        return ""
    digits = re.sub(r"[^\d.]", "", str(v))
    if not digits:
        return str(v)
    try:
        n = float(digits)
    except ValueError:
        return str(v)
    return "₹" + (f"{int(n):,}" if n == int(n) else f"{n:,.2f}")


def _level(v) -> str:
    """A bare price level with the rupee sign added — never to a % or a point count."""
    t = str(v or "").strip()
    if not t or not re.fullmatch(
            r"[\d,]+(?:\.\d+)?(?:\s*(?:-|/|to)\s*[\d,]+(?:\.\d+)?)*", t, re.I):
        return t
    return "₹" + re.sub(r"\s*([-/])\s*", r"\1", t)


def _band(v) -> str:
    """A price band as a range with both ends marked: "94 to 99" -> "₹94 – ₹99"."""
    t = re.sub(r"(?i)\s*(?:/|per\s+)\s*(?:share|shr|equity)\b", "", str(v or "")).strip()
    nums = re.findall(r"\d[\d,]*\.?\d*", t)
    if len(nums) == 2:
        return f"₹{nums[0]} – ₹{nums[1]}"
    return _level(t) or t


def _company(name) -> str:
    """A company name without the suffix the sentence is about to supply."""
    n = re.sub(r"(?i)\s*(?<![A-Za-z])(ipo|ltd\.?|limited)\s*$", "",
               str(name or "").strip())
    return n.strip(" -–—:") or str(name or "").strip()


def _cost(f: dict) -> str:
    if f.get("application_amount"):
        return _rupees(f["application_amount"])
    prices = [float(x.replace(",", ""))
              for x in re.findall(r"\d[\d,]*\.?\d*", str(f.get("price_band") or ""))]
    lots = re.findall(r"\d+", str(f.get("lot_size") or ""))
    if prices and lots:
        return _rupees(max(prices) * int(lots[0]))
    return ""


def _market_blocks(f: dict, tone_when_stale: str = "risk") -> list:
    """Turn a market check into blocks. Silent when nothing could be checked.

    This is the part that answers a channel's "for educational purposes only". The
    system does not argue with the disclaimer — it states what the market actually says
    and lets the two sit side by side, which is the only reply a disclaimer deserves.
    """
    m = f.get("_market") or {}
    if not m:
        return []
    if not m.get("checked"):
        return [B.Note(
            f"We could not check this against the market — "
            f"{m.get('why', 'no data available')}. The figures above are the channel's "
            f"alone.", "neutral")]

    out = []
    findings = [x for x in (m.get("findings") or []) if x]
    if findings:
        out.append(B.Note(" ".join(findings[:3]),
                          tone_when_stale if m.get("stale") else "neutral"))
    news = m.get("news") or []
    if news:
        out.append(B.Facts([((n.get("source") or "news")[:22], n["title"][:84])
                            for n in news[:3]], "In the news this fortnight"))
    return out


def _confirmed(n) -> str:
    if n is None:
        return ""
    return f"Reported by {n} channels" if n >= 2 else "Only one channel so far"


def ipo_new(ctx: dict) -> Email:
    f, who = ctx.get("fields") or {}, ctx.get("trust") or {}
    name = _company(ctx.get("company") or f.get("company")) or "A company"
    cost, close = _cost(f), f.get("close_date")
    chan = _channel(who.get("title"))

    subject = f"Apply by {close} — {name} IPO" if close else f"New IPO — {name}"

    opening = (f"{name} is selling shares to the public for the first time. "
               + (f"You can apply through Angel One or Groww until {close}."
                  if close else "You can apply through Angel One or Groww."))
    if str(f.get("board") or "").lower() == "sme":
        opening += " It is a smaller company than most, which makes it riskier."

    required = []
    if cost:
        required.append(B.Hero(cost, "What one lot costs",
                               "Held in your bank account, not taken"))
    required.append(B.Para(opening))
    required.append(B.Steps([
        "Open Angel One or Groww and go to the IPO section.",
        f"Pick {name}, choose one lot, and tick the cut-off price box.",
        "Approve the payment request in your bank app within 30 minutes.",
    ]))

    rows = []
    if f.get("price_band"):
        rows.append(("Price band", f"{_band(f['price_band'])} per share"))
    if f.get("lot_size"):
        rows.append(("Shares in one lot", str(f["lot_size"])))
    if cost:
        rows.append(("You pay", cost))
    if close:
        rows.append(("Closes", str(close)))
    if f.get("allotment_date"):
        rows.append(("You find out", str(f["allotment_date"])))
    if f.get("listing_date"):
        rows.append(("Starts trading", str(f["listing_date"])))

    # Order is priority. The numbers ARE the alert — a price band, a lot size and a
    # closing date are what the reader needs; "the money is held rather than taken" is
    # reassurance. The reassurance was listed first and the budget dropped the table,
    # so an IPO email went out with no price band in it.
    optional = []
    if rows:
        optional.append(B.Facts(rows, "The numbers"))
    if close:
        optional.append(B.Note(
            "Apply a day before this closes. Your broker stops accepting applications "
            "earlier than the exchange does.", "warn"))
    if cost:
        optional.append(B.Para(
            "The money is held in your account rather than taken. If you are not given "
            "any shares, the hold is released within three working days."))
    if f.get("gmp"):
        optional.append(B.Note(
            f"Unofficial grey-market chatter puts it around {f['gmp']} above the issue "
            "price. That figure moves daily, it is not a price you can trade at, and "
            "plenty of IPOs open below what their buyers paid.", "neutral"))
    optional += _market_blocks(f, tone_when_stale="neutral")
    # Attribution is REQUIRED, never optional. It was last in the optional list and
    # the word budget dropped it, so an email arrived with no statement of who said it —
    # which is the one thing this system must always add.
    required.append(B.Source(chan, _tier(who),
                             _confirmed(f.get("_corroborating_sources"))))

    return _assemble("ipo_new", "New IPO", B.BLUE, subject, required, optional,
                     protect=(chan,),
                     meta={"company": name, "cost": cost, "deadline": close})


def ipo_allotment(ctx: dict) -> Email:
    f, who = ctx.get("fields") or {}, ctx.get("trust") or {}
    name = _company(ctx.get("company") or f.get("company")) or "your IPO"
    chan = _channel(who.get("title"))

    required = [
        B.Para(f"The results are out for {name}. You can now find out whether you were "
               f"given any shares."),
        B.Steps([
            "Open Angel One or Groww and go to IPO, then Orders.",
            "It will say Allotted or Not Allotted next to the company.",
        ], "How to check"),
    ]
    optional = [
        B.Para("If you were not given any, the money held in your bank is released "
               "within three working days and there is nothing for you to do."),
        B.Para("If you were, the shares appear in your account before trading opens. "
               "You can sell them on the first day or keep them — both are fine."),
    ]
    if f.get("listing_date"):
        optional.append(B.Facts([("Starts trading", str(f["listing_date"]))]))
    required.append(B.Source(chan, _tier(who)))

    return _assemble("ipo_allotment", "Allotment result", B.GREEN,
                     f"{name}: allotment is out", required, optional,
                     protect=(chan,), meta={"company": name})


def ipo_listing(ctx: dict) -> Email:
    f, who = ctx.get("fields") or {}, ctx.get("trust") or {}
    name = _company(ctx.get("company") or f.get("company")) or "your IPO"
    chan = _channel(who.get("title"))

    required = [
        B.Para(f"{name} shares start trading today. If you were given any, they are in "
               f"your account now and you can sell them whenever you like."),
        B.Para("Holding is a decision too. Nothing forces you to sell on the first day, "
               "and the first day's price is not a verdict on the company — new shares "
               "often swing hard before they settle."),
    ]
    optional = []
    if f.get("price_band"):
        optional.append(B.Facts([("What you paid",
                                  f"{_band(f['price_band'])} per share")]))
    optional += [
        B.Steps([
            "Open your app and find the share under Holdings.",
            "Sell at the market price, or set the price you want and wait.",
        ], "If you want to sell"),
    ]
    required.append(B.Source(chan, _tier(who)))
    return _assemble("ipo_listing", "Now trading", B.BLUE,
                     f"{name} starts trading today", required, optional,
                     protect=(chan,), meta={"company": name})


def gmp_change(ctx: dict) -> Email:
    f, who = ctx.get("fields") or {}, ctx.get("trust") or {}
    name = _company(ctx.get("company") or f.get("company")) or "An IPO"
    gmp = f.get("gmp") or "changed"
    chan = _channel(who.get("title"))

    required = [
        B.Hero(str(gmp), "Grey-market premium", f"Unofficial, and only for {name}"),
        B.Para("This is what unofficial traders say the shares might open at, above the "
               "issue price. It is not a price you can buy or sell at, nobody regulates "
               "it, and it moves every day until listing."),
        B.Note("Do not apply because of this number alone. One channel you follow has "
               "quoted a badly wrong grey-market figure before.", "risk"),
    ]
    optional = []
    if f.get("close_date"):
        optional.append(B.Facts([("Applications close", str(f["close_date"]))]))
    # Attribution is REQUIRED, never optional. It was last in the optional list and
    # the word budget dropped it, so an email arrived with no statement of who said it —
    # which is the one thing this system must always add.
    required.append(B.Source(chan, _tier(who),
                             _confirmed(f.get("_corroborating_sources"))))

    return _assemble("gmp_change", "Grey-market move", B.AMBER,
                     f"{name}: grey-market estimate now {gmp}", required, optional,
                     protect=(chan,), meta={"company": name})


def regulatory(ctx: dict) -> Email:
    f, who = ctx.get("fields") or {}, ctx.get("trust") or {}
    reg = f.get("regulator") or "The regulator"
    what = f.get("what_changed") or ctx.get("headline") or "A market rule has changed."
    action = (f.get("action_needed") or "").strip()
    deadline = f.get("deadline")
    chan = _channel(who.get("title"))

    required = []
    if deadline:
        required.append(B.Hero(str(deadline), "You have until",
                               "After this the new rule applies"))
    required.append(B.Para(what))
    if action and not action.lower().startswith("nothing"):
        required.append(B.Steps([action], "What you need to do"))
    else:
        required.append(B.Para("There is nothing for you to do. This is for "
                               "information."))

    optional = []
    if f.get("who_it_affects"):
        optional.append(B.Facts([("Who this applies to", str(f["who_it_affects"]))]))
    if f.get("affects_retail") is False:
        optional.append(B.Note("This looks like a rule for institutions rather than "
                               "individual investors, so it probably changes nothing "
                               "for you.", "neutral"))
    optional += [
        B.Note("Rules get garbled when they are forwarded between channels. Before you "
               "act on this, check the regulator's own website.", "warn"),
    ]
    required.append(B.Source(chan, _tier(who)))
    return _assemble("regulatory", f"{reg} rule change", B.AMBER,
                     f"{reg}: {what}", required, optional,
                     footer="This is a rule change, not advice. Confirm it at the source.",
                     protect=(chan, reg), meta={"regulator": reg, "deadline": deadline})


def trade_call(ctx: dict) -> Email:
    """The reader's own messages, delivered. The system adds context, never withholds."""
    f, who = ctx.get("fields") or {}, ctx.get("trust") or {}
    chan = _channel(who.get("title"))
    sym = f.get("symbol") or "an unnamed stock"
    direction = str(f.get("direction") or "").upper()
    has_sl = f.get("stop_loss") not in (None, "", "null")

    said = f"{chan} says {direction.lower() or 'to trade'} {sym}"
    if f.get("entry"):
        said += f" at {_level(f['entry'])}"
    if f.get("target"):
        said += f", expecting {_level(f['target'])}"
    said += "."

    required = []
    if has_sl:
        required.append(B.Hero(
            _level(f["stop_loss"]), "Their stop-loss",
            "The price at which they would sell and accept the loss"))
    required.append(B.Para(said))
    if not has_sl:
        required.append(B.Note(
            "They gave no stop-loss, so they have not said at what point they would "
            "accept the trade had gone wrong. Nothing here limits the loss. This is the "
            "single biggest quality difference between the channels you follow.",
            "risk"))

    rows = []
    label = {"direction": "Action", "instrument": "Type", "entry": "Entry",
             "target": "Target", "target_2": "Second target",
             "stop_loss": "Stop-loss", "timeframe": "Holding period"}
    for k in ("direction", "instrument", "entry", "target", "target_2", "stop_loss",
              "timeframe"):
        if f.get(k) in (None, ""):
            continue
        v = _level(f[k]) if k in ("entry", "target", "target_2", "stop_loss") else f[k]
        rows.append((label[k], str(v).capitalize() if k == "direction" else str(v)))

    # The market check goes high — directly under the call, before the channel's own
    # numbers are laid out. If the price has left their entry behind, that is the first
    # thing the reader should know.
    optional = list(_market_blocks(f))
    if f.get("symbol"):
        kind = {"options": "an option contract", "futures": "a futures contract",
                "index": "a market index"}.get(str(f.get("instrument") or ""),
                                               "a share traded in India")
        optional.append(B.Para(f"{sym} is {kind}."))
    if rows:
        optional.append(B.Facts(rows, "The call"))
    if who.get("note"):
        optional.append(B.Note(str(who["note"])[:200], "neutral"))
    if ctx.get("sl_rate") is not None:
        optional.append(B.Note(f"This channel gives a stop-loss on {ctx['sl_rate']}% of "
                               f"the calls we have recorded from it.", "neutral"))
    required.append(B.Source(chan, _tier(who)))

    subject = f"{chan}: {direction.lower() or 'call on'} {sym}".strip()
    return _assemble("trade_call", "Trade call", B.RED, subject, required, optional,
                     protect=(chan, sym),
                     meta={"symbol": sym, "has_stop_loss": has_sl, "channel": chan})


def unclassified_escalation(ctx: dict) -> Email:
    f, who = ctx.get("fields") or {}, ctx.get("trust") or {}
    about = ctx.get("about") or ctx.get("headline") or "A message we could not place."
    chan = _channel(who.get("title"))

    required = [
        B.Para(f"{str(about).rstrip('.')}."),
        B.Para("We could not work out which category this belongs to, so we are passing "
               "it on rather than filing it away. It may well be nothing."),
    ]
    optional = []
    if f.get("time_sensitive"):
        optional.append(B.Note("This mentions a date or a deadline, so it may not wait.",
                               "warn"))
    if ctx.get("raw"):
        optional.append(B.Quote(str(ctx["raw"])[:400], chan))
    if f.get("entities"):
        optional.append(B.Facts([("Mentions",
                                  ", ".join(str(e) for e in f["entities"][:5]))]))
    required.append(B.Source(chan, _tier(who)))

    return _assemble("unclassified_escalation", "Not sure about this", B.INK_SOFT,
                     f"Worth a look: {about}", required, optional,
                     footer="Shown because we would rather waste your time than miss "
                            "something.",
                     protect=(chan,))


def daily_digest(ctx: dict) -> Email:
    day = ctx.get("date") or "today"
    seen, items = set(), []
    for i in ctx.get("items") or []:
        h = str(i.get("headline") or i.get("text") or "").strip()
        low = h.lower()
        if not h or len(h) < 12 or "could not" in low or low in seen:
            continue
        seen.add(low)
        items.append(h)

    footer = "A round-up of low-priority items. Nothing here is advice."
    if not items:
        return _assemble(
            "daily_digest", "Daily round-up", B.INK_SOFT,
            f"Nothing worth reporting — {day}",
            [B.Para("Your channels were quiet today. Nothing came through that needed "
                    "your attention.")], [], footer=footer, meta={"count": 0})

    required = [B.Para("These came through your channels today. None of them needed an "
                       "email of their own, and none needs a decision from you.")]
    optional = [B.Facts([(f"{i}.", h[:110]) for i, h in enumerate(items[:8], 1)])]

    return _assemble("daily_digest", "Daily round-up", B.INK_SOFT,
                     f"{len(items)} things worth knowing — {day}", required, optional,
                     footer=footer, meta={"count": len(items)})


def system_health(ctx: dict) -> Email:
    ok = bool(ctx.get("ok", True))
    required = [
        B.Para("Your market alerts are running normally. There is nothing for you to "
               "do — this note exists so that silence never leaves you guessing."
               if ok else
               "Something is wrong with your market alerts, and some messages may not "
               "have reached you.")
    ]
    if not ok:
        required.append(B.Note(
            ctx.get("what_to_do")
            or "Nothing to do yet. If this warning arrives again tomorrow, the alerts "
               "have stopped and need a look.", "warn"))

    rows = [("Messages read", str(ctx.get("events", 0))),
            ("Emails sent", str(ctx.get("sent", 0))),
            ("Problems", "none" if not ctx.get("errors") else str(ctx["errors"]))]
    if ctx.get("last_message"):
        rows.append(("Newest message", str(ctx["last_message"])))

    return _assemble("system_health", "Daily check", B.GREEN if ok else B.AMBER,
                     "Your alerts are running normally" if ok
                     else "Problem with your alerts",
                     required, [B.Facts(rows, "Yesterday")],
                     footer="Sent once a day so that silence never means the system died.",
                     meta={"ok": ok})


TEMPLATES = {
    "ipo_new": ipo_new,
    "ipo_allotment": ipo_allotment,
    "ipo_listing": ipo_listing,
    "gmp_change": gmp_change,
    "regulatory": regulatory,
    "trade_call": trade_call,
    "unclassified_escalation": unclassified_escalation,
    "daily_digest": daily_digest,
    "system_health": system_health,
}


def render(kind: str, ctx: dict) -> Email:
    if kind not in TEMPLATES:
        raise KeyError(f"no template {kind!r}; have {sorted(TEMPLATES)}")
    return TEMPLATES[kind](ctx)


def from_event(ev: dict, outcome: dict) -> Email:
    """Pick a template from a decided event and its sub-agent outcome."""
    f = outcome.get("fields") or {}
    intent = ev.get("intent") or ""
    agent = outcome.get("agent")

    # An IPO agent that extracted nothing has no IPO to report. Rendering it anyway
    # produced "Allotment out — your IPO: check now": an email that looks exactly like a
    # real alert and contains nothing to act on. Show the message and say we could not
    # read it.
    if agent == "ipo" and not (f.get("company") or "").strip():
        agent = "escalation"

    if agent == "ipo":
        kind = ("gmp_change" if intent == "ipo_gmp" else
                "ipo_allotment" if intent == "ipo_allotment" else
                "ipo_listing" if intent == "ipo_listing" else "ipo_new")
    elif agent == "trade_call":
        kind = "trade_call"
    elif agent == "regulatory":
        kind = "regulatory"
    elif outcome.get("action") == "digest":
        kind = "daily_digest"
    else:
        kind = "unclassified_escalation"

    about = (outcome.get("headline") or "").split(": ", 1)[-1]
    if kind == "unclassified_escalation":
        raw = " ".join((ev.get("text") or "").split())
        about = (raw[:90] if raw
                 else "a message with no text — the content is in the attachment")

    ctx = {"fields": f, "headline": outcome.get("headline"), "about": about,
           "raw": ev.get("text"),
           "trust": {"title": _channel(ev.get("channel_title")), "tier": ev.get("tier")},
           "items": ([{"headline": outcome.get("headline")}]
                     if kind == "daily_digest" else [])}
    return render(kind, ctx)

