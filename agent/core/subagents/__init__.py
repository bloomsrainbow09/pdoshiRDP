"""The sub-agent machinery every vertical inherits.

What lives here is the GUARANTEE, not the mapping:

  base.run()    the no-drop contract — a sub-agent that raises, returns None, or is
                handed an empty message still produces an Outcome with a reason, and
                that Outcome escalates rather than vanishing.
  base.ask()    one model call through the role's cross-provider chain, logged, with a
                retry at double budget when a reasoning model truncates mid-JSON.
  Outcome       the shape every agent returns; `action` is the only field the
                orchestrator must read.
  escalation    the universal fallback. An intent nobody claims still reaches the reader.

What does NOT live here is which agent handles which intent. `ipo_gmp -> ipo` is a
statement about one niche and lives in `verticals/<name>/subagents/__init__.py`. Ask the
plugin: `registry.current().subagent_for(intent)`.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parents[1]), str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import base                     # noqa: E402
import escalation               # noqa: E402

Outcome = base.Outcome
run = base.run
ask = base.ask
trust = base.trust

__all__ = ["base", "escalation", "Outcome", "run", "ask", "trust"]
