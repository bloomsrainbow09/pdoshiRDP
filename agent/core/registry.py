"""Resolve a vertical name to its plugin. The only bridge from `core` to `verticals`.

This module imports `verticals/<name>/plugin.py` **by path**, not by package name. That is
deliberate: a static `import verticals.trading` anywhere in `core` is exactly the coupling
this refactor exists to remove, and `gates/gate_arch.py` fails the build on one. A
path-based load keeps the dependency dynamic and by-name, which is what "plugin" means.

Loading is strict. A vertical that does not satisfy the contract raises immediately and
names every missing member. The alternative — a partially loaded plugin that works until
some unlucky message reaches the one method it lacks, six hours into an unattended run —
is the failure mode this system can least afford.

    from core import registry
    plugin = registry.load("trading")
    plugin.render("trade_call", payload)
"""

import importlib.util
import sys
from pathlib import Path

from . import contract

ROOT = Path(__file__).resolve().parent.parent
VERTICALS = ROOT / "verticals"

_CACHE: dict = {}


def available() -> list:
    """Every vertical that has a plugin. `_template` is a skeleton, not a vertical."""
    if not VERTICALS.is_dir():
        return []
    return sorted(p.name for p in VERTICALS.iterdir()
                  if p.is_dir() and not p.name.startswith("_")
                  and (p / "plugin.py").is_file())


def load(name: str):
    """Import and validate `verticals/<name>/plugin.py`. Cached per process."""
    if name in _CACHE:
        return _CACHE[name]

    path = VERTICALS / name / "plugin.py"
    if not path.is_file():
        known = ", ".join(available()) or "(none)"
        raise FileNotFoundError(
            f"no vertical '{name}': expected {path}. Known verticals: {known}. "
            f"Copy verticals/_template/ to start a new one.")

    # The vertical's own modules import each other as flat names (`import market`,
    # `import glossary`), matching how the rest of this codebase is laid out. Its folder
    # goes on sys.path so those resolve, and the project root goes on so it can reach
    # `core` and `engine`.
    for p in (str(ROOT), str(path.parent)):
        if p not in sys.path:
            sys.path.insert(0, p)

    mod_name = f"_vertical_{name}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution so a plugin module that imports itself transitively
    # does not re-enter this loader and recurse.
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise

    plugin = getattr(module, "PLUGIN", None) or module
    if not getattr(plugin, "name", None):
        try:
            plugin.name = name
        except AttributeError:
            pass

    contract.verify(plugin)
    _CACHE[name] = plugin
    return plugin


def reset() -> None:
    """Drop the cache. For tests that reload a plugin after editing it."""
    _CACHE.clear()


def current():
    """The plugin for the vertical this process is serving.

    The one call site every consumer in `core` uses. Keeping it here rather than making
    each caller write `load(active.name())` means the "which vertical am I" decision has
    exactly one implementation.
    """
    from . import active
    return load(active.name())
