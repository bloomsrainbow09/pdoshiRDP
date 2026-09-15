"""Async LLM client — the one path every model call in this system takes.

Design notes, all driven by running unattended for weeks:

* **Fallback chains are mandatory, not optional.** On this NVIDIA key `gemma-3-12b`,
  `phi-3-vision` and `vila` already return 404, and `llama-3.2-90b-vision` times out.
  A single model disappearing must degrade the pipeline, never stop it.
* **Every call is metered.** Latency, tokens and estimated cost are returned with the
  result so `content.agent_runs` can hold the real numbers. Unattended spend is how
  budgets die quietly.
* **Structured output is validated, not trusted.** Models emit fenced JSON, prose
  preambles and trailing commentary. `parse_json` handles all three, and the caller
  validates against a Pydantic model.
* **Bounded concurrency.** 48 channels can fire at once; an unbounded gather would
  trip provider rate limits and blow the runner's memory.
"""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS = ROOT.parent / "keys" / "CREDENTIALS.env"
ENV_FILE = ROOT / ".env.telegram-app"

# Three independent providers, deliberately. A fallback chain that stays inside one
# provider does not survive the failure mode actually observed here: a KEY-level 429
# that takes out every model on that key at once. Cross-provider chains do.
PROVIDERS = {
    "nvidia": {"url": "https://integrate.api.nvidia.com/v1/chat/completions",
               "models_url": "https://integrate.api.nvidia.com/v1/models",
               "keys": ["NVIDIA_API_KEY", "NVIDIA_API_KEY_2"]},
    "kilo": {"url": "https://api.kilocode.ai/api/openrouter/chat/completions",
             "models_url": "https://api.kilocode.ai/api/openrouter/models",
             "keys": ["KILOCODE_TOKEN"],
             # OpenRouter-style gateway: it wants attribution headers or it throttles.
             "headers": {"HTTP-Referer": "https://kilocode.ai", "X-Title": "tg-agent"}},
    "zai": {"url": "https://api.z.ai/api/paas/v4/chat/completions",
            "models_url": "https://api.z.ai/api/paas/v4/models",
            "keys": ["ZAI_API_KEY"]},
}


def _parse_env(path: Path) -> dict:
    if not path.is_file():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.split(" #")[0].split("\t#")[0].strip()
    return out


def env() -> dict:
    """Project env wins over shared credentials; real env vars win over both."""
    merged = {**_parse_env(CREDENTIALS), **_parse_env(ENV_FILE)}
    merged.update({k: v for k, v in os.environ.items()
                   if k.startswith(("NVIDIA_", "ZAI_", "KILOCODE_", "OCR_")) and v.strip()})
    return merged


def key_for(provider: str) -> str:
    e = env()
    for name in PROVIDERS[provider]["keys"]:
        if e.get(name):
            return e[name]
    raise RuntimeError(f"no API key for provider '{provider}'")


@dataclass
class Result:
    ok: bool
    text: str = ""
    model: str = ""
    provider: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""
    attempts: list = field(default_factory=list)
    finish_reason: str = ""
    reasoning: str = ""          # some models put their chain-of-thought here

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def truncated(self) -> bool:
        """Hit the token ceiling before finishing.

        Matters because REASONING models (glm-5.3-flash, deepseek-v4, kimi) spend the
        budget in `reasoning_content` and return content='' with finish_reason='length'.
        That looks like success — HTTP 200, no exception — but yields nothing. Measured:
        glm-5.3-flash produced empty content on 176/200 messages at max_tokens=80.
        Surfacing it here is what stops it being a silent failure.
        """
        return self.finish_reason == "length" or (self.ok and not self.text.strip())


JSON_FENCE = re.compile(r"^```(?:json)?|```$", re.M)


def parse_json(raw: str):
    """Models wrap JSON in fences, prose, or both. Recover it or return None."""
    t = JSON_FENCE.sub("", (raw or "").strip()).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", t, re.S)          # largest brace-span
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


async def call_one(client: httpx.AsyncClient, provider: str, model: str,
                   system: str, user: str, *, max_tokens: int = 800,
                   temperature: float = 0.0, timeout: float = 90.0,
                   json_mode: bool = False, retries: int = 3) -> Result:
    """One model, with backoff on transient failures.

    429 handling is not optional on this provider. Measured: the NVIDIA free tier
    rate-limits per KEY on sustained load — a 200-message burst that succeeds for the
    first model starts returning 429 for the next, even at concurrency 1. Production
    volume (~2k messages/day, ~1.4/min) is far below that, but bursts on startup gap-
    replay will hit it, so the backoff is load-bearing.

    Never raises — failure comes back as Result(ok=False).
    """
    last = None
    for attempt in range(retries):
        res = await _call_once(client, provider, model, system, user,
                               max_tokens=max_tokens, temperature=temperature,
                               timeout=timeout, json_mode=json_mode)
        if res.ok:
            return res
        last = res
        transient = ("HTTP 429" in res.error or "HTTP 5" in res.error
                     or "Timeout" in res.error or "empty content" in res.error)
        if not transient or attempt == retries - 1:
            return res
        # 2s, 6s, 18s — long enough for a per-minute window to roll over
        await asyncio.sleep(2 * (3 ** attempt))
    return last


async def _call_once(client: httpx.AsyncClient, provider: str, model: str,
                     system: str, user: str, *, max_tokens: int = 800,
                     temperature: float = 0.0, timeout: float = 90.0,
                     json_mode: bool = False) -> Result:
    body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
            "messages": ([{"role": "system", "content": system}] if system else [])
                        + [{"role": "user", "content": user}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    t0 = time.perf_counter()
    try:
        headers = {"Authorization": f"Bearer {key_for(provider)}",
                   "Content-Type": "application/json", "Accept": "application/json",
                   **PROVIDERS[provider].get("headers", {})}
        r = await client.post(PROVIDERS[provider]["url"], json=body,
                              timeout=timeout, headers=headers)
        ms = int((time.perf_counter() - t0) * 1000)
        if r.status_code != 200:
            return Result(False, model=model, provider=provider, latency_ms=ms,
                          error=f"HTTP {r.status_code}: {r.text[:160]}")
        d = r.json()
        usage = d.get("usage") or {}
        # A 200 that carries no `choices` is a provider returning an error body with the
        # wrong status. Kilo did exactly this and the caller saw "KeyError: 'choices'",
        # which says nothing about what happened. Report the body instead.
        if not (isinstance(d, dict) and d.get("choices")):
            return Result(False, model=model, provider=provider, latency_ms=ms,
                          error=f"HTTP 200 but no choices: {str(d)[:160]}")
        choice = d["choices"][0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        res = Result(True, text=text, model=model, provider=provider, latency_ms=ms,
                     prompt_tokens=usage.get("prompt_tokens", 0),
                     completion_tokens=usage.get("completion_tokens", 0),
                     finish_reason=choice.get("finish_reason") or "",
                     reasoning=reasoning)
        # Empty content with a full reasoning buffer is a failure, not a success.
        # Report it as one so a fallback chain actually fails over instead of
        # propagating an empty string downstream.
        if not text.strip():
            res.ok = False
            res.error = (f"empty content (finish_reason={res.finish_reason}, "
                         f"{len(reasoning)} reasoning chars) — raise max_tokens "
                         f"or use a non-reasoning model")
        return res
    except Exception as e:
        return Result(False, model=model, provider=provider,
                      latency_ms=int((time.perf_counter() - t0) * 1000),
                      error=f"{type(e).__name__}: {str(e)[:140]}")


async def call(chain: list, system: str, user: str, *, client=None, **kw) -> Result:
    """Try each (provider, model) in order until one succeeds.

    `chain` is [(provider, model), ...] — the fallback chain from models.json.
    Every attempt is recorded on the Result so failures stay visible in agent_runs
    rather than being hidden by a successful fallback.
    """
    own = client is None
    client = client or httpx.AsyncClient()
    attempts = []
    try:
        # Models the circuit breaker has benched go to the BACK of the chain, never out
        # of it. A provider that failed three times in a row is probably down, so trying
        # it first wastes a timeout on every message; but a chain emptied by breakers
        # cannot deliver anything, and losing a message is the one outcome this system
        # exists to prevent. Import is local: llm.py is the lowest layer and must not
        # depend on the rest of the agent at module scope.
        try:
            from resilience import BREAKER
            ordered = BREAKER.order(list(chain))
        except Exception:
            BREAKER, ordered = None, list(chain)

        for provider, model in ordered:
            res = await call_one(client, provider, model, system, user, **kw)
            attempts.append(f"{model}:{'ok' if res.ok else res.error[:60]}")
            if BREAKER is not None:
                BREAKER.record(model, res.ok, res.error or "")
            if res.ok:
                res.attempts = attempts
                return res
        out = Result(False, error="entire chain failed", attempts=attempts)
        return out
    finally:
        if own:
            await client.aclose()


async def map_bounded(items: list, fn, limit: int = 8) -> list:
    """Run fn over items with bounded concurrency, preserving order.

    48 channels can fire simultaneously; unbounded gather trips provider rate limits
    and grows memory without bound on a 2 GB runner.
    """
    sem = asyncio.Semaphore(limit)

    async def guarded(i, item):
        async with sem:
            return i, await fn(item)

    done = await asyncio.gather(*(guarded(i, it) for i, it in enumerate(items)))
    return [r for _, r in sorted(done, key=lambda x: x[0])]


async def list_models(provider: str = "nvidia") -> list:
    async with httpx.AsyncClient() as c:
        r = await c.get(PROVIDERS[provider]["models_url"], timeout=45,
                        headers={"Authorization": f"Bearer {key_for(provider)}",
                                 **PROVIDERS[provider].get("headers", {})})
        r.raise_for_status()
        return sorted(m["id"] for m in r.json().get("data", []))


# ---------------------------------------------------------------------------
# Cassette layer — record/replay, opt-in via the CASSETTE environment variable.
#
# When CASSETTE is unset or "off" this block does nothing at all: `call_one` is not
# rebound and the production path is byte-for-byte what it was before. That matters
# because the restructure's whole claim is that behaviour did not change, and a
# permanently-installed interceptor would be a change.
#
# The seam is `call_one` rather than `_call_once`, so that on replay the per-model
# backoff does not burn wall-clock re-serving a recorded failure. The fallback chain
# and the circuit breaker in `call()` still run either way, so their behaviour is
# exercised and frozen rather than bypassed.
#
# See agent/cassette.py for why a replay miss is a hard error.
if (os.environ.get("CASSETTE") or "off").strip().lower() != "off":
    import cassette as _cassette                                        # noqa: E402

    _call_one_live = call_one

    async def call_one(client, provider, model, system, user, **kw):    # noqa: F811
        return await _cassette.through(_call_one_live, Result, client,
                                       provider, model, system, user, **kw)
