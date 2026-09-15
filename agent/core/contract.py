"""The Plugin contract — the only thing `core` is allowed to know about a vertical.

`engine/verticals.py` already opens with "a niche defined as data, not code". That was
true for channels and prompts and never became true for the agent layer: the taxonomy,
the sub-agent registry, the nine email templates, the 85-term glossary, the market
verifier and the material-signal regex all ended up in the spine. Adding a second vertical
meant forking it.

This module is the seam. `core` imports this file and nothing else from the vertical side;
a vertical imports whatever it likes from `core`. The direction is enforced by
`gates/gate_arch.py`, which parses the import graph rather than grepping it.

Every member below earned its place by being something `core` genuinely cannot decide:

  taxonomy              which intents exist at all, and what delivery action each implies.
                        Derived from a corpus, so it is data, and the corpus is per-niche.

  subagent_for          which module handles an intent. Returns the escalation fallback
                        for anything unclaimed — NEVER None. An intent nobody owns must
                        still reach the user; that guarantee predates this refactor and
                        survives it unchanged.

  has_material_signal   the code-level veto. In trading this is a regex for price levels,
                        targets and stop-losses, and it overrules three separate discard
                        paths. It is vertical knowledge by definition: `core` cannot know
                        what "worth keeping" looks like in a niche it has never seen.
                        A vertical with no such notion returns False and loses nothing.

  guard/router/         the wording of the three pipeline prompts. `core` owns the
  importance_prompt     scaffolding — roles, the intent list, the event block — because
                        that is structure. What counts as noise in this niche is not.

  render                one email. Composes `core.templates.blocks`, which owns the
                        palette, the 600px table layout and both renderers. The vertical
                        owns which blocks, in what order, saying what.

  render_event          the delivery-facing form: an event plus its outcome, straight to
                        an email. Which template an outcome maps to is vertical knowledge,
                        so `send.py` must not be the one deciding it.

  glossary_terms        which jargon this email has to footnote. A vertical whose readers
                        already speak the language returns [].

  enrich                optional external verification — trading checks a call against the
                        live market so a disclaimer cannot launder a claim about prices.
                        `core` time-boxes it and treats the result as opaque. Returns None
                        for a vertical with nothing to check.

  boilerplate           dedupe stop-words. The near-duplicate scorer compares FACTS, and
                        which words carry no fact is niche-specific ("ipo", "allotment"
                        mean nothing in a recipe vertical).

  urgent_intents        which intents may break quiet hours. Derived from the taxonomy,
                        but a vertical may narrow it.

  dedupe_extra          the niche-specific tail of a dedupe key. intent + entity + day
                        is universal; whether a moved GMP or a different entry level
                        makes it a NEW fact is not.

Nothing else belongs here. A member that `core` could compute itself is coupling, not
contract.
"""

from typing import Protocol, runtime_checkable

# Every callable a conforming plugin must expose. `gates/gate_arch.py` and
# `tests/test_contract.py` both read this rather than re-listing the members, so adding
# to the protocol automatically tightens both.
REQUIRED = (
    "subagent_for",
    "droppable",
    "has_material_signal",
    "guard_prompt",
    "router_prompt",
    "importance_prompt",
    "render",
    "render_event",
    "glossary_terms",
    "enrich",
    "boilerplate",
    "urgent_intents",
    "dedupe_extra",
)

REQUIRED_ATTRS = ("name", "taxonomy")


@runtime_checkable
class Plugin(Protocol):
    """What a vertical must provide. See the module docstring for why each exists."""

    name: str
    taxonomy: dict

    # ---- routing -----------------------------------------------------------
    def subagent_for(self, intent: str):
        """The module that handles `intent`. Never None — unclaimed intents escalate."""

    def droppable(self) -> set:
        """Intents the guard may terminate without a sub-agent ever seeing them."""

    def has_material_signal(self, text: str) -> bool:
        """True if this text carries something a reader could act on.

        Consulted at every door that can discard a message. Returning False always is a
        valid implementation for a vertical with no such concept.
        """

    # ---- prompting ---------------------------------------------------------
    def guard_prompt(self) -> str: ...
    def router_prompt(self) -> str: ...
    def importance_prompt(self) -> str: ...

    # ---- rendering ---------------------------------------------------------
    def render(self, kind: str, payload: dict):
        """One rendered email. Composes core.templates.blocks."""

    def render_event(self, ev: dict, outcome: dict):
        """One delivered email, straight from a pipeline event and its outcome.

        Separate from `render(kind, payload)` because CHOOSING the template is itself
        vertical knowledge: an ipo_gmp outcome becomes a gmp_change email, a failed
        extraction becomes an escalation. `core/delivery/send.py` must be able to turn an
        event into an email without knowing any of that.
        """

    def glossary_terms(self, text: str, already=()) -> list:
        """(term, explanation) pairs this text needs footnoted. [] is valid."""

    # ---- optional enrichment ----------------------------------------------
    def enrich(self, kind: str, fields: dict):
        """External verification of an extracted claim, or None.

        `core` bounds this with a timeout and never inspects the result — it is handed
        back to the vertical's own renderer.
        """

    # ---- delivery tuning ---------------------------------------------------
    def boilerplate(self) -> set:
        """Words the dedupe signature must ignore because they carry no fact."""

    def urgent_intents(self) -> set:
        """Intents allowed to break quiet hours."""

    def dedupe_extra(self, intent: str, fields: dict) -> str:
        """What else distinguishes two events of this intent, beyond entity and day.

        `core` builds the dedupe key from intent + subject + day, which is universal.
        Whether two events that agree on all three are actually the SAME fact is not:
        in trading, a grey-market premium that moved is new information while the same
        premium repeated is not, and one stock called at different levels is two calls.
        A vertical with no such distinction returns "".
        """


class ContractError(TypeError):
    """A vertical does not satisfy the protocol.

    Always names the missing members. A partially-loaded plugin that fails later, deep in
    the pipeline, on one unlucky message, is far worse than one that refuses to load.
    """


def missing(plugin) -> list:
    """Which required members `plugin` lacks. Empty list means conforming."""
    out = []
    for attr in REQUIRED_ATTRS:
        if not hasattr(plugin, attr):
            out.append(f"{attr} (attribute)")
    for fn in REQUIRED:
        f = getattr(plugin, fn, None)
        if f is None or not callable(f):
            out.append(f"{fn}()")
    return out


def verify(plugin) -> None:
    """Raise ContractError unless `plugin` satisfies the protocol."""
    gaps = missing(plugin)
    if gaps:
        name = getattr(plugin, "name", plugin.__class__.__name__)
        raise ContractError(
            f"vertical '{name}' does not satisfy the Plugin contract — missing: "
            + ", ".join(gaps)
            + ". See core/contract.py for what each member is for.")
