"""Prompt scaffolding. Structure, not wording.

The split this module represents: `core` owns how a prompt is *assembled* — how an event
becomes a uniform block, how a taxonomy becomes a list the model can choose from, which
intents the taxonomy marks as droppable. What counts as noise in a particular niche is
the vertical's business, and lives in `verticals/<name>/wording.py`.

Nothing here mentions a market, a company or an instrument. If it did, a second vertical
would inherit a stock-market prompt with its own nouns swapped in, which is exactly the
fork this refactor exists to prevent.

The output of `intent_list()` is byte-identical to the `_intent_list()` it replaced, and
`user_block()` is moved verbatim. That is load-bearing: the cassette hashes the assembled
prompt, so any drift here becomes a replay MISS rather than a silent behaviour change.
"""


def intent_list(taxonomy: dict) -> str:
    """The taxonomy as a choosable list, commonest intent first.

    Ordering by frequency is deliberate and is part of the prompt: the model sees the
    distribution it is actually classifying, with `unknown` sitting high because ~30% of
    real traffic genuinely is unknown.
    """
    intents = taxonomy["intents"]
    lines = []
    for name, spec in sorted(intents.items(), key=lambda kv: -kv[1]["freq_pct"]):
        lines.append(f"- {name:<16} {spec['description']}  [~{spec['freq_pct']}% of traffic]")
    return "\n".join(lines)


def droppable(taxonomy: dict) -> set:
    """Intents the taxonomy declares terminal-by-discard.

    Derived, never hand-listed. A category cannot become silently droppable by someone
    editing a set in one file and forgetting the other five.
    """
    return {k for k, v in taxonomy["intents"].items() if v["action"] == "discard"}


def user_block(ev: dict) -> str:
    """Uniform message rendering. Channel and tier are included because trust is not
    uniform — a tier1 claim and a tier3 claim are not equal evidence."""
    bits = [f"channel: {ev.get('channel_title') or '?'}",
            f"tier: {ev.get('tier') or '?'}"]
    if ev.get("has_media"):
        bits.append(f"has {ev.get('media_kind') or 'media'}")
    if ev.get("urls"):
        bits.append(f"{len(ev['urls'])} link(s)")
    head = "[" + " · ".join(bits) + "]"
    text = (ev.get("text") or "").strip() or "(no text — media only)"
    return f"{head}\n\n{text[:1800]}"
