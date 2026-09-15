"""Which vertical this process is serving, and how to read its data files.

This exists to break a cycle. `verticals/trading/plugin.py` imports the pipeline modules
it forwards to; if those modules then asked the registry for their taxonomy, loading the
plugin would re-enter the loader through them. Reading the JSON **by path** costs nothing
and has no import edge at all, so both sides can reach the same file without either
importing the other.

**One vertical per process, resolved from the environment.** That is not a limitation
introduced here — it is what the system already does: `watcher.py` takes `vertical=` as a
parameter and holds one Telegram lease per account, and the orchestrator drains one queue.
Serving several verticals from one process is a real feature, but it is a *change in
behaviour*, and this refactor is not allowed to make one. `VERTICAL` defaults to
`trading`, which is what every existing call site assumed.

    from core import active
    active.name()            # "trading"
    active.taxonomy()        # verticals/trading/taxonomy.json, parsed and cached
"""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERTICALS = ROOT / "verticals"

DEFAULT = "trading"
_CACHE: dict = {}


def name() -> str:
    """The vertical this process serves. `VERTICAL` env var, else `trading`."""
    return (os.environ.get("VERTICAL") or DEFAULT).strip() or DEFAULT


def path(vertical: str | None = None) -> Path:
    return VERTICALS / (vertical or name())


def data(filename: str, vertical: str | None = None) -> dict:
    """Read and cache a JSON file from the vertical's folder.

    Raises with the expected path rather than a bare KeyError further downstream — a
    vertical missing its taxonomy should fail at startup, naming the file, not six
    layers into a message.
    """
    v = vertical or name()
    ck = (v, filename)
    if ck in _CACHE:
        return _CACHE[ck]
    p = path(v) / filename
    if not p.is_file():
        known = sorted(q.name for q in VERTICALS.iterdir() if q.is_dir()) \
            if VERTICALS.is_dir() else []
        raise FileNotFoundError(
            f"vertical '{v}' has no {filename} (expected {p}). "
            f"Verticals present: {', '.join(known) or '(none)'}")
    _CACHE[ck] = json.loads(p.read_text(encoding="utf-8"))
    return _CACHE[ck]


def taxonomy(vertical: str | None = None) -> dict:
    """The intent taxonomy. Was `agent/config/taxonomy.json`; now per-vertical."""
    return data("taxonomy.json", vertical)


def reset() -> None:
    _CACHE.clear()


def module(name: str, vertical: str | None = None):
    """Import `verticals/<name>/<module>.py` by path, cached.

    The vertical's own modules import each other as flat names (`import glossary`,
    `import market`), matching how the rest of this codebase is laid out. Callers in
    `core` and in the gates cannot rely on that, so they come through here instead of
    guessing at sys.path — which is what broke when R5 moved glossary.py out of
    agent/templates/ and four call sites kept importing it as a top-level module.
    """
    import importlib.util
    import sys

    v = vertical or globals()["name"]()
    key = ("__module__", v, name)
    if key in _CACHE:
        return _CACHE[key]

    p = path(v) / f"{name}.py"
    if not p.is_file():
        raise FileNotFoundError(f"vertical '{v}' has no {name}.py (expected {p})")

    for extra in (str(ROOT), str(path(v))):
        if extra not in sys.path:
            sys.path.insert(0, extra)

    mod_name = f"_v_{v}_{name}"
    if mod_name in sys.modules:
        _CACHE[key] = sys.modules[mod_name]
        return _CACHE[key]
    spec = importlib.util.spec_from_file_location(mod_name, p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    _CACHE[key] = mod
    return mod
