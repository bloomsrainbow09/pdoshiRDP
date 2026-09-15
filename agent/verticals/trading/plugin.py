"""The trading vertical, as a plugin.

**This starts life as a pure forwarder.** Every member delegates to code that still lives
in `agent/`; nothing has moved yet. That is deliberate and is the point of R1: it proves
the contract in `core/contract.py` is *sufficient* before a single file is relocated. If
some member cannot be expressed as a forward, the contract is wrong — and finding that out
now costs nothing, whereas finding it out in R5 costs a half-finished move.

R2 through R6 hollow this file out from below: as each piece of trading logic moves into
this folder, the corresponding forward becomes a local call. By R6 there are no forwards
left and `agent/` no longer contains anything this vertical needs.

The `sys.path` juggling below is temporary for the same reason — `agent/` lays its modules
out flat (`import render`, `import glossary`) and the forwarder has to reach them. It goes
away with the last forward.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
AGENT = ROOT / "agent"

# Temporary: until R5 finishes, the implementations are still under agent/.
for _p in (str(HERE), str(ROOT), str(AGENT), str(AGENT / "templates"), str(AGENT / "delivery")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import active as _active   # noqa: E402

VERTICAL_NAME = "trading"



# Every module in this folder is loaded BY PATH under a vertical-unique name, never as a
# flat `import render`. Each vertical has its own render.py, wording.py, glossary.py and
# so on, and Python resolves a flat import against sys.modules first — so whichever
# vertical loaded first would silently hand its modules to every other one. That is not
# hypothetical: `registry.load("trading")` was returning DEMO's renderer, and
# `sys.modules["render"]` pointed at verticals/demo/render.py. The subagents package hit
# the same trap in R7; this is the rest of it.
def _own(mod: str):
    return _active.module(mod, VERTICAL_NAME)


name = "trading"

_wording = _own('wording')
_glossary = _own('glossary')
_market = _own('market')
_boilerplate = _own('boilerplate')
_render = _own('render')
_signals = _own('signals')

def _own_subagents():
    """Load THIS vertical's subagents package under a unique module name.

    `core/subagents/` and `verticals/<n>/subagents/` are both called `subagents`, so a
    flat `import subagents` resolves to whichever entered sys.modules first — and the
    orchestrator imports core's before any plugin loads. The result was the plugin
    silently getting core's machinery package, which has no REGISTRY, and gate P07
    failing with `module 'subagents' has no attribute 'DROPPABLE'`.

    Loading by path under a per-vertical name means two verticals can both have a
    `subagents` package and neither shadows the other.
    """
    import importlib.util
    import sys as _s
    pkg = HERE / "subagents"
    mod_name = f"_subagents_{HERE.name}"
    if mod_name in _s.modules:
        return _s.modules[mod_name]
    spec = importlib.util.spec_from_file_location(
        mod_name, pkg / "__init__.py", submodule_search_locations=[str(pkg)])
    mod = importlib.util.module_from_spec(spec)
    _s.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        _s.modules.pop(mod_name, None)
        raise
    return mod


_subagents = _own_subagents()


# Read from this folder's own taxonomy.json, not from `_prompts.TAXONOMY`. Both end up
# with the same dict, but going through the loader means the plugin does not depend on a
# pipeline module for its own data — which is what lets R3 move the prompts out without
# touching this line.
taxonomy = _active.taxonomy("trading")


# ---- routing ---------------------------------------------------------------------
# R4: no longer a forward. The mapping (`ipo_gmp -> ipo`) is a statement about this niche
# and lives beside the agents it names. `base.run()`'s no-drop guarantee and `escalation`
# stay in the spine.
def subagent_for(intent: str):
    """Never None: an intent nobody claims still escalates to a human-readable email."""
    return _subagents.for_intent(intent)


def droppable() -> set:
    return set(_subagents.DROPPABLE)


def has_material_signal(text: str) -> bool:
    """A price level, a target, a stop-loss — something a reader could act on.

    Overrules all three discard doors. See `signals.py` for the regex and D-14/D-15 for
    why it exists: the guard was killing 6 of 10 real trade calls.

    R5: this was the last forward into agent/. Moving it here is what lets the
    orchestrator import the registry instead of the other way round.
    """
    return _signals.has_material_signal(text)


# ---- prompting -------------------------------------------------------------------
# R3: these are no longer forwards. The wording lives in this folder; core/prompts.py
# keeps only the scaffolding (how an event becomes a block, how a taxonomy becomes a
# choosable list). All three strings are byte-identical to what agent/prompts.py held.
def guard_prompt() -> str:
    return _wording.GUARD


def router_prompt() -> str:
    return _wording.ROUTER


def importance_prompt() -> str:
    return _wording.IMPORTANCE


# ---- rendering -------------------------------------------------------------------
def render(kind: str, payload: dict):
    return _render.render(kind, payload)


def render_event(ev: dict, outcome: dict):
    """Which template an outcome maps to is our decision, not the delivery loop's."""
    return _render.from_event(ev, outcome)


def glossary_terms(text: str, already=()) -> list:
    return _glossary.terms_in(text, already)


# ---- optional enrichment ---------------------------------------------------------
def enrich(kind: str, fields: dict):
    """Check an extracted claim against the live market.

    A channel posting `BUY X target 500` under "for educational purposes only" is still
    making a claim about prices, and prices are checkable. Answering the disclaimer with
    a politely-worded caveat would accept the channel's framing; this does not.

    Returns None for kinds with nothing to verify — `core` treats the result as opaque
    and only ever hands it back to this vertical's renderer.
    """
    if kind == "trade_call":
        return _market.check_call(fields, company=fields.get("company") or "")
    if kind.startswith("ipo"):
        return _market.check_ipo(fields)
    return None


# ---- delivery tuning -------------------------------------------------------------
def boilerplate() -> set:
    """Words that carry no fact, so the dedupe signature must ignore them.

    R6: no longer a forward. The scorer and its calibrated 0.50 threshold stayed in
    core/delivery/policy.py; only the vocabulary is ours.
    """
    return set(_boilerplate.WORDS)


def urgent_intents() -> set:
    """Intents allowed to break quiet hours, derived from our own taxonomy."""
    return {k for k, v in taxonomy["intents"].items() if v.get("quiet_hours_override")}


def dedupe_extra(intent: str, fields: dict) -> str:
    """What makes two same-day events about one company genuinely different facts.

    Moved verbatim out of core/delivery/policy.py:dedupe_key in R6. `normalise` is
    core's, so the produced string is byte-identical to what it was before the move.
    """
    from core.delivery.policy import normalise
    f = fields or {}
    if intent == "ipo_gmp" and f.get("gmp"):
        # A grey-market premium that MOVED is a new fact; one repeated is not.
        return ":" + normalise(str(f["gmp"]))[:16]
    if intent == "trade_call":
        # Two channels calling the same stock at the same levels is one call; the same
        # stock at different levels is genuinely two.
        return ":" + normalise(f"{f.get('entry','')}-{f.get('target','')}")[:24]
    return ""


# ---- optional hooks ---------------------------------------------------------------
# Not in the REQUIRED contract: `core` reads them with getattr and has a working default
# without them. They exist so `core` names no market concept, not because every vertical
# needs them.

def ledger_agents() -> set:
    """Agents whose outcomes are written to the accuracy ledger.

    A trade call is a checkable prediction — entry, target, stop-loss — so it gets a row
    in `content.trade_calls` and can be scored later. An IPO announcement is a fact, not
    a prediction, and is not ledgered. A vertical that makes no checkable predictions
    defines nothing here and nothing is written.
    """
    return {"trade_call"}


def name_suffixes() -> tuple:
    """Niche suffixes to strip before comparing two company names.

    `core` already strips the universal ones (Ltd, Limited, Pvt, Inc…). Channels here
    write "Sonaselection IPO", and that must still match "Sonaselection" from a channel
    that does not — corroboration counting depends on it.
    """
    return ("ipo",)
