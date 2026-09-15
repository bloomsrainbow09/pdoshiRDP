"""Record/replay for model calls — the thing that makes this refactor provable.

The pipeline calls LLMs, so its output is nondeterministic, so "the refactor changed
nothing" cannot be asserted by re-running it. This module removes the nondeterminism by
recording every model response once and serving it from a file thereafter.

**The seam is `llm.call_one()`** — the single function that actually performs an HTTP
request. Intercepting there rather than at `llm.call()` is deliberate: the fallback chain,
the circuit breaker and the per-model retry all still execute, so their behaviour is part
of what gets frozen instead of being bypassed.

**A replay MISS is a hard error, never a fallthrough to the network.** That is the whole
mechanism. If a refactor changes one character of an assembled prompt, the request hash
changes, the lookup misses, and the run fails loudly — which is precisely the signal R3
(splitting the prompts) depends on. Degrading a miss to a live call would convert the
one reliable detector in this design into a silent pass.

Modes, via the `CASSETTE` environment variable:

    unset / "off"   no interception at all. `llm.call_one` is not even rebound, so the
                    production path is byte-for-byte what it was before this file existed.
    "record"        perform the real call, write the Result to the cassette, return it.
    "replay"        serve from the cassette; raise `CassetteMiss` if absent.

Cassette files live in `baseline/cassettes/<name>.jsonl` (one JSON object per line, so a
long recording can be appended to and inspected with `grep`). They are gitignored because
recorded responses quote channel text verbatim.
"""

import hashlib
import json
import os
from dataclasses import asdict, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "baseline" / "cassettes"


class CassetteMiss(RuntimeError):
    """Replay was asked for a call that was never recorded.

    Nearly always means a prompt, model chain or token budget changed. The message
    carries the provider, model and a prompt digest so the culprit is identifiable
    without dumping the whole prompt into a traceback.
    """


def mode() -> str:
    return (os.environ.get("CASSETTE") or "off").strip().lower()


def name() -> str:
    return (os.environ.get("CASSETTE_NAME") or "default").strip()


def path() -> Path:
    return DIR / f"{name()}.jsonl"


# --------------------------------------------------------------------------- keying
# The hash covers everything that can change a model's answer. `client` is excluded (a
# connection pool, not an input) and so is `timeout`/`retries` (transport policy: they
# change how hard we try, never what we ask).
_KEYED = ("max_tokens", "temperature", "json_mode")


def key(provider: str, model: str, system: str, user: str, **kw) -> str:
    payload = {
        "provider": provider,
        "model": model,
        "system": system,
        "user": user,
        **{k: kw.get(k) for k in _KEYED},
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _digest(text: str, n: int = 12) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:n]


# --------------------------------------------------------------------------- storage
_CACHE: dict | None = None


def _load() -> dict:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    _CACHE = {}
    p = path()
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Later entries win: a re-recording of the same key supersedes the older one.
            _CACHE[row["key"]] = row["result"]
    return _CACHE


def reset() -> None:
    """Drop the in-process cache. Used by tests that switch cassettes mid-run."""
    global _CACHE
    _CACHE = None


def _append(k: str, result_dict: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    with path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"key": k, "result": result_dict}, ensure_ascii=False) + "\n")
    _load()[k] = result_dict


def stats() -> dict:
    return {"mode": mode(), "name": name(), "entries": len(_load()), "path": str(path())}


# --------------------------------------------------------------------------- the wrapper
def _to_result(cls, d: dict):
    """Rebuild a Result, tolerating fields added to the dataclass after a recording."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in known})


async def through(live_fn, result_cls, client, provider: str, model: str,
                  system: str, user: str, **kw):
    """Run `live_fn` under the active cassette mode.

    `result_cls` is passed in rather than imported so this module stays free of any
    dependency on `llm`, which imports it — the cycle would otherwise be unavoidable.
    """
    m = mode()
    if m == "off":
        return await live_fn(client, provider, model, system, user, **kw)

    k = key(provider, model, system, user, **kw)

    if m == "replay":
        hit = _load().get(k)
        if hit is None:
            raise CassetteMiss(
                f"no recording for {provider}/{model} "
                f"[system:{_digest(system)} user:{_digest(user)} "
                f"max_tokens={kw.get('max_tokens')} temp={kw.get('temperature')}] "
                f"in {path().name} ({len(_load())} entries). "
                f"A miss means the request changed — usually a prompt edit.")
        return _to_result(result_cls, hit)

    if m == "record":
        hit = _load().get(k)
        if hit is not None:
            return _to_result(result_cls, hit)
        res = await live_fn(client, provider, model, system, user, **kw)
        _append(k, asdict(res))
        return res

    raise ValueError(f"CASSETTE={m!r} is not one of: off, record, replay")
