"""The content model every email is built from, and the two renderers for it.

A template describes WHAT it wants to say as a list of blocks. `to_text` and `to_html`
then render the same blocks two ways, so the plain-text part and the HTML part cannot
drift apart — they are the same content, not two hand-maintained copies.

Three problems with the previous templates are fixed here by the model itself:

**Labels were sentence fragments.** "YOU FIND OUT: 22 Sep." and "IT STARTS TRADING:
25 Sep." are not how anyone writes. Numbers now live in a FACTS table where a short
label is correct, and everything else is written as real sentences in PARA blocks.

**Glosses were shoved mid-sentence.** "Choose 1 lot (the fixed bundle of shares you must
buy at once) at the cut-off price (ticking this lets the app pay the final price,
whatever it lands on)" is unreadable. Terms are now marked where they appear and
explained once in a small block at the end, so the sentence stays a sentence.

**Everything had the same visual weight.** The deadline looked exactly like the listing
date. There is now one HERO — the number that decides whether the reader acts — and
everything else is arranged beneath it.

Palette from the `ui-ux-pro-max` catalogue, row 1 (trust blue on a light ground). Light,
not dark: email clients invert unpredictably and a dark email prints as a black page.
"""

import html as _html
import re
from dataclasses import dataclass, field

# ── palette ───────────────────────────────────────────────────────────────────
INK = "#1E293B"          # body text
INK_SOFT = "#475569"     # secondary text
INK_FAINT = "#94A3B8"    # captions
LINE = "#E2E8F0"         # hairlines
PAPER = "#FFFFFF"        # card
WASH = "#F8FAFC"         # page ground
TINT = "#EFF4FB"         # subtle panel fill
BLUE = "#2563EB"         # primary / trust
AMBER = "#B45309"        # deadline, caution (readable on light, unlike #EA580C)
AMBER_BG = "#FEF6E7"
RED = "#B91C1C"          # risk
RED_BG = "#FEF2F2"
GREEN = "#15803D"        # confirmed
GREEN_BG = "#F0FDF4"

FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',"
        "Arial,sans-serif")

TONE = {"neutral": (INK_SOFT, TINT, LINE),
        "warn": (AMBER, AMBER_BG, "#F5D8A8"),
        "risk": (RED, RED_BG, "#F5C2C2"),
        "good": (GREEN, GREEN_BG, "#BBF7D0")}


# ── blocks ────────────────────────────────────────────────────────────────────

@dataclass
class Hero:
    """The one number that decides whether the reader acts. At most one per email."""
    value: str
    caption: str = ""
    sub: str = ""


@dataclass
class Para:
    """A real sentence. Prose, not a labelled fragment."""
    text: str


@dataclass
class Steps:
    """What to do, in order. Numbered because the order matters."""
    items: list
    title: str = "What to do"


@dataclass
class Facts:
    """Numbers. The ONE place a short label is the right way to write."""
    rows: list                      # [(label, value), ...]
    title: str = ""


@dataclass
class Note:
    """Something the reader must not assume. `tone` decides the colour."""
    text: str
    tone: str = "neutral"


@dataclass
class Quote:
    """The reader's own message, verbatim. Never rewritten, always marked as theirs."""
    text: str
    source: str = ""


@dataclass
class Source:
    """Who said it and how much that is worth."""
    channel: str
    tier_phrase: str
    confirmed: str = ""


@dataclass
class Words:
    """Terms explained once, at the end, out of the way of the sentences."""
    terms: list                     # [(term, meaning), ...]


@dataclass
class Rendered:
    text: str
    html: str
    prose: str                      # the PARA text only — what readability judges
    words: int


# ── text renderer ─────────────────────────────────────────────────────────────

def _wrap(s: str, width: int = 74, indent: str = "") -> str:
    out, line = [], indent
    for word in s.split():
        if len(line) + len(word) + 1 > width and line.strip():
            out.append(line.rstrip())
            line = indent + word + " "
        else:
            line += word + " "
    if line.strip():
        out.append(line.rstrip())
    return "\n".join(out)


def to_text(blocks: list, footer: str) -> tuple:
    parts, prose = [], []
    for b in blocks:
        if isinstance(b, Hero):
            head = b.caption.upper() if b.caption else ""
            parts.append((head + "\n" if head else "") + b.value
                         + (f"\n{b.sub}" if b.sub else ""))
        elif isinstance(b, Para):
            parts.append(_wrap(b.text))
            prose.append(b.text)
        elif isinstance(b, Steps):
            body = "\n".join(_wrap(f"{i}. {s}", indent="   ")[3:] if False
                             else f"{i}. " + _wrap(s, 70, "   ")[3:]
                             for i, s in enumerate(b.items, 1))
            parts.append(f"{b.title.upper()}\n{body}")
            prose.extend(b.items)
        elif isinstance(b, Facts):
            width = max((len(l) for l, _ in b.rows), default=0)
            body = "\n".join(f"  {l.ljust(width)}   {v}" for l, v in b.rows)
            parts.append((b.title.upper() + "\n" if b.title else "") + body)
        elif isinstance(b, Note):
            parts.append(_wrap(b.text))
            prose.append(b.text)
        elif isinstance(b, Quote):
            head = f"THEIR MESSAGE{' — ' + b.source if b.source else ''}"
            parts.append(head + "\n" + _wrap(b.text, 70, "  "))
        elif isinstance(b, Source):
            line = f"From {b.channel} — {b.tier_phrase}."
            if b.confirmed:
                line += " " + b.confirmed
            parts.append(_wrap(line))
            prose.append(line)
        elif isinstance(b, Words):
            body = "\n".join(_wrap(f"{t} — {m}", 70, "  ")[2:] if False
                             else "  " + _wrap(f"{t} — {m}", 70, "  ")[2:]
                             for t, m in b.terms)
            parts.append("WORDS USED HERE\n" + body)
    text = "\n\n".join(p for p in parts if p.strip()) + "\n\n" + footer
    return text, "\n".join(prose)


# ── html renderer ─────────────────────────────────────────────────────────────

def _e(s) -> str:
    return _html.escape(str(s), quote=False)


def _row(inner: str, pad: str = "0 28px") -> str:
    return (f'<tr><td style="padding:{pad};font-family:{FONT}">{inner}</td></tr>')


def to_html(blocks: list, subject: str, kind_label: str, accent: str,
            footer: str) -> str:
    """Table-based, inline-styled, 600px. Written for mail clients, not browsers.

    No flexbox and no grid: Outlook renders neither. No web fonts, no images, no
    remote anything — many clients block it by default and a template that depends on
    it degrades to nothing.
    """
    out = []
    for b in blocks:
        if isinstance(b, Hero):
            cap = (f'<div style="font-size:11px;letter-spacing:1.2px;font-weight:700;'
                   f'color:{INK_FAINT};text-transform:uppercase;margin:0 0 6px">'
                   f'{_e(b.caption)}</div>') if b.caption else ""
            sub = (f'<div style="font-size:14px;color:{INK_SOFT};margin:6px 0 0">'
                   f'{_e(b.sub)}</div>') if b.sub else ""
            out.append(_row(
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
                f' style="background:{TINT};border:1px solid {LINE};border-radius:10px">'
                f'<tr><td style="padding:18px 20px;font-family:{FONT}">{cap}'
                f'<div style="font-size:30px;line-height:1.15;font-weight:700;'
                f'color:{INK}">{_e(b.value)}</div>{sub}</td></tr></table>',
                pad="8px 28px"))
        elif isinstance(b, Para):
            out.append(_row(
                f'<p style="margin:0 0 14px;font-size:16px;line-height:1.6;color:{INK}">'
                f'{_e(b.text)}</p>'))
        elif isinstance(b, Steps):
            lis = "".join(
                f'<li style="margin:0 0 9px;padding-left:4px">{_e(s)}</li>'
                for s in b.items)
            out.append(_row(
                f'<div style="font-size:11px;letter-spacing:1.2px;font-weight:700;'
                f'color:{INK_FAINT};text-transform:uppercase;margin:4px 0 10px">'
                f'{_e(b.title)}</div>'
                f'<ol style="margin:0 0 14px;padding-left:22px;font-size:16px;'
                f'line-height:1.55;color:{INK}">{lis}</ol>'))
        elif isinstance(b, Facts):
            trs = ""
            for i, (l, v) in enumerate(b.rows):
                bg = PAPER if i % 2 == 0 else WASH
                trs += (f'<tr><td style="padding:9px 14px;font-size:14px;color:{INK_SOFT};'
                        f'background:{bg};border-bottom:1px solid {LINE}">{_e(l)}</td>'
                        f'<td align="right" style="padding:9px 14px;font-size:15px;'
                        f'font-weight:600;color:{INK};background:{bg};'
                        f'border-bottom:1px solid {LINE}">{_e(v)}</td></tr>')
            title = (f'<div style="font-size:11px;letter-spacing:1.2px;font-weight:700;'
                     f'color:{INK_FAINT};text-transform:uppercase;margin:4px 0 8px">'
                     f'{_e(b.title)}</div>') if b.title else ""
            out.append(_row(
                title + f'<table role="presentation" width="100%" cellpadding="0" '
                f'cellspacing="0" style="border:1px solid {LINE};border-radius:8px;'
                f'border-collapse:separate;overflow:hidden;margin:0 0 14px">'
                f'{trs}</table>'))
        elif isinstance(b, Note):
            fg, bg, br = TONE.get(b.tone, TONE["neutral"])
            out.append(_row(
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
                f' style="background:{bg};border:1px solid {br};border-radius:8px;'
                f'margin:0 0 12px"><tr><td style="padding:12px 14px;font-family:{FONT};'
                f'font-size:14px;line-height:1.55;color:{fg}">{_e(b.text)}</td></tr>'
                f'</table>'))
        elif isinstance(b, Quote):
            src = (f'<div style="font-size:12px;color:{INK_FAINT};margin:0 0 6px">'
                   f'{_e(b.source)}</div>') if b.source else ""
            out.append(_row(
                f'<div style="font-size:11px;letter-spacing:1.2px;font-weight:700;'
                f'color:{INK_FAINT};text-transform:uppercase;margin:4px 0 8px">'
                f'Their message</div>'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
                f' style="background:{WASH};border-left:3px solid {INK_FAINT};'
                f'margin:0 0 14px"><tr><td style="padding:12px 14px;font-family:{FONT};'
                f'font-size:14px;line-height:1.55;color:{INK_SOFT};'
                f'white-space:pre-wrap">{src}{_e(b.text)}</td></tr></table>'))
        elif isinstance(b, Source):
            badge = ""
            if b.confirmed:
                good = b.confirmed.lower().startswith(("confirmed", "reported by"))
                fg, bg, br = TONE["good"] if good else TONE["warn"]
                badge = (f'<span style="display:inline-block;padding:3px 9px;'
                         f'border-radius:999px;background:{bg};border:1px solid {br};'
                         f'color:{fg};font-size:12px;font-weight:600">'
                         f'{_e(b.confirmed)}</span>')
            out.append(_row(
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
                f' style="border-top:1px solid {LINE};margin:4px 0 0"><tr>'
                f'<td style="padding:12px 0 0;font-family:{FONT};font-size:13px;'
                f'color:{INK_SOFT}">From <strong style="color:{INK}">{_e(b.channel)}'
                f'</strong><br><span style="color:{INK_FAINT}">{_e(b.tier_phrase)}'
                f'</span></td><td align="right" style="padding:12px 0 0;'
                f'font-family:{FONT}">{badge}</td></tr></table>'))
        elif isinstance(b, Words):
            items = "".join(
                f'<div style="margin:0 0 6px"><strong style="color:{INK}">{_e(t)}</strong>'
                f'<span style="color:{INK_SOFT}"> — {_e(m)}</span></div>'
                for t, m in b.terms)
            out.append(_row(
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
                f' style="background:{WASH};border:1px solid {LINE};border-radius:8px;'
                f'margin:14px 0 0"><tr><td style="padding:12px 14px;font-family:{FONT};'
                f'font-size:13px;line-height:1.5">'
                f'<div style="font-size:11px;letter-spacing:1.2px;font-weight:700;'
                f'color:{INK_FAINT};text-transform:uppercase;margin:0 0 8px">'
                f'Words used here</div>{items}</td></tr></table>'))

    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
        f' style="background:{WASH};margin:0;padding:0"><tr><td align="center"'
        f' style="padding:20px 10px">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0"'
        f' style="width:100%;max-width:600px;background:{PAPER};border:1px solid {LINE};'
        f'border-radius:12px;overflow:hidden">'
        # accent strip + kind label
        f'<tr><td style="height:4px;background:{accent};font-size:0;line-height:0">'
        f'&nbsp;</td></tr>'
        f'<tr><td style="padding:18px 28px 4px;font-family:{FONT}">'
        f'<div style="font-size:11px;letter-spacing:1.4px;font-weight:700;color:{accent};'
        f'text-transform:uppercase">{_e(kind_label)}</div>'
        f'<h1 style="margin:8px 0 14px;font-size:21px;line-height:1.3;font-weight:700;'
        f'color:{INK}">{_e(subject)}</h1></td></tr>'
        + "".join(out) +
        f'<tr><td style="padding:18px 28px 22px;font-family:{FONT}">'
        f'<div style="border-top:1px solid {LINE};padding-top:12px;font-size:12px;'
        f'line-height:1.5;color:{INK_FAINT}">{_e(footer)}</div></td></tr>'
        f'</table></td></tr></table>')


def render_blocks(blocks: list, subject: str, kind_label: str, accent: str,
                  footer: str) -> Rendered:
    text, prose = to_text(blocks, footer)
    html = to_html(blocks, subject, kind_label, accent, footer)
    return Rendered(text, html, prose,
                    len(re.findall(r"[A-Za-z][A-Za-z'\-]*", text)))
