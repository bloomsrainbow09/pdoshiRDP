"""Sub-agent registry for the TRADING vertical: which agent handles which intent.

Moved out of `agent/subagents/__init__.py` in R4. The mapping is the clearest example of
something that cannot live in the spine — `ipo_gmp -> ipo` is a statement about this niche
and nothing else. What stays in the spine is the *guarantee* around it: `base.run()`'s
no-drop contract, and `escalation` as the fallback for anything unclaimed.

`for_intent()` returns escalation rather than None, and that is load-bearing rather than
defensive. An intent nobody claims must still reach the user; a None here would surface as
a crash or a silent drop deep in the pipeline, six hours into an unattended run. Gate P07
asserts the mapping is complete against this vertical's taxonomy, so adding an intent
later cannot quietly leave messages unhandled.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[2]
# `base` and `escalation` stay in the spine; this folder holds the vertical's own agents.
# _HERE.parent is the vertical root, where market.py, glossary.py and signals.py sit.
# `base` and `escalation` live in core/subagents — the no-drop guarantee and the
# universal fallback are machinery every vertical inherits. _HERE.parent is this
# vertical's root, where market.py, glossary.py and signals.py sit.
for _p in (str(_ROOT), str(_ROOT / "core"), str(_ROOT / "core" / "subagents"),
           str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import base                     # noqa: E402  core: Outcome, run(), the no-drop guarantee
import escalation               # noqa: E402  core: the universal fallback
import ipo                      # noqa: E402
import news                     # noqa: E402
import regulatory               # noqa: E402
import trade_call               # noqa: E402

Outcome = base.Outcome
run = base.run

REGISTRY = {
    "ipo_new":       ipo,
    "ipo_allotment": ipo,
    "ipo_gmp":       ipo,
    "ipo_listing":   ipo,
    "trade_call":    trade_call,
    "regulatory":    regulatory,
    "stock_news":    news,
    "results":       news,
    "market_news":   news,
    "institutional": news,
    "education":     escalation,
    "unknown":       escalation,
    # `promotional`, `performance`, `greeting` and `fragment` are terminated by the
    # guard and never reach a sub-agent. They are listed in gate P07's exemption set
    # rather than mapped here, so that the mapping stays a statement about DELIVERY.
}

DROPPABLE = {"promotional", "performance", "greeting", "fragment", "filtered"}


def for_intent(intent: str):
    """The agent that owns this intent. Unowned intents escalate — never None."""
    return REGISTRY.get(intent, escalation)


async def dispatch(client, ev: dict, routed: dict):
    """Route one event to its sub-agent under the no-drop guarantee in base.run()."""
    return await base.run(for_intent((routed or {}).get("intent")), client, ev, routed)


__all__ = ["REGISTRY", "DROPPABLE", "Outcome", "dispatch", "for_intent", "run",
           "ipo", "news", "regulatory", "escalation", "trade_call", "base"]
