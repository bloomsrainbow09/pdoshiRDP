"""OCR for chart and call screenshots, via a hosted vision model.

Why a vision model rather than tesseract: these are not clean documents. They are
broker screenshots, TradingView charts with overlaid annotations, and posts in
Gujarati script -- all of which classical OCR handles badly. A VLM also returns the
numbers already *structured* (symbol, price, IPO dates, GMP) in one pass, which is
what the render stage actually needs.

Providers are OpenAI-compatible chat endpoints, so switching model is one config
line. Keys resolve from .env.telegram-app first (this project keeps its own copy so
it can be shipped to the EC2 or a runner as one file), then ../keys/CREDENTIALS.env,
then real environment variables, which win. Rotating a key means updating both
files. Model names are deliberately NOT hard-coded -- `run.py media models` lists
what the key currently reaches, since the catalogue changes.
"""

import base64
import json
import mimetypes
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from engine import config

PROVIDERS = {
    "nvidia": {
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "models_url": "https://integrate.api.nvidia.com/v1/models",
        "key": "NVIDIA_API_KEY",
        "key_alt": "NVIDIA_API_KEY_2",
        # Benchmarked 2026-09-13 on a synthetic IPO card (gmp 45 / lot 150 / dates):
        #   gemma-4-31b-it   98s   every field correct          <- default
        #   llama-3.2-11b   141s   read GMP 45 as 46            <- fallback only
        #   llama-3.2-90b     --   times out even at 240s
        #   gemma-3-12b, phi-3-vision, vila   404 for this key
        # The 11b's silent digit error is why numbers get verified below and why the
        # stronger model is preferred despite costing more time.
        "default_model": "google/gemma-4-31b-it",
        "fallbacks": ["meta/llama-3.2-11b-vision-instruct"],
    },
    "zai": {
        "url": "https://api.z.ai/api/paas/v4/chat/completions",
        "models_url": "https://api.z.ai/api/paas/v4/models",
        "key": "ZAI_API_KEY",
        "default_model": "glm-4.5v",
        "fallbacks": ["glm-4v-flash"],
    },
}

PROMPT = """You are reading a screenshot from an Indian stock-market Telegram channel.

Return ONLY a JSON object, no prose, no code fence:
{
  "text": "every word visible in the image, in reading order, original script kept",
  "kind": "ipo | trade_call | chart | news | pnl | promo | other",
  "symbols": ["stock or index names shown"],
  "numbers": {"price": null, "target": null, "stoploss": null, "gmp": null,
              "lot_size": null, "issue_price": null, "open_date": null, "close_date": null},
  "language": "en | gu | hi | mixed",
  "summary": "one factual sentence describing what the image shows"
}

Rules:
- Transcribe what is actually there. Never infer a number that is not visible.
- Keep Gujarati or Hindi text in its own script inside "text".
- Use null for anything absent. Do not guess.
- "summary" must be descriptive, never advice.
"""


def _env() -> dict:
    """This project's own .env.telegram-app wins; ../keys/CREDENTIALS.env is the
    fallback so an older checkout without the copied keys still works. Real
    environment variables beat both, which is how a runner supplies them."""
    merged = dict(config.parse_env_file(config.CREDENTIALS))
    merged.update(config.parse_env_file(config.ENV_FILE))
    for k in ("NVIDIA_API_KEY", "NVIDIA_API_KEY_2", "ZAI_API_KEY",
              "KILOCODE_TOKEN", "OCR_PROVIDER", "OCR_MODEL"):
        v = os.environ.get(k, "").strip()
        if v:
            merged[k] = v
    return merged


def defaults() -> tuple:
    """(provider, model) honouring OCR_PROVIDER / OCR_MODEL if set."""
    env = _env()
    provider = env.get("OCR_PROVIDER") or "nvidia"
    if provider not in PROVIDERS:
        raise SystemExit(f"OCR_PROVIDER '{provider}' unknown; pick from {list(PROVIDERS)}")
    return provider, (env.get("OCR_MODEL") or None)


def _key(provider: str) -> str:
    env = _env()
    p = PROVIDERS[provider]
    k = env.get(p["key"]) or env.get(p.get("key_alt", ""), "")
    if not k:
        raise SystemExit(
            f"{p['key']} not set. Add it to {config.ENV_FILE.name} "
            f"(or {config.CREDENTIALS.name}), or export it.")
    return k


def list_models(provider: str = "nvidia", vision_only: bool = True) -> list:
    p = PROVIDERS[provider]
    req = urllib.request.Request(p["models_url"],
                                 headers={"Authorization": f"Bearer {_key(provider)}"})
    data = json.loads(urllib.request.urlopen(req, timeout=45).read())
    ids = sorted(m["id"] for m in data.get("data", []))
    if not vision_only:
        return ids
    pat = re.compile(r"(vl|vision|vila|ocr|llama-4|gemma-3|phi-3.*vision|glm-4v|glm-4\.5v)", re.I)
    return [i for i in ids if pat.search(i)]


def _call(provider: str, model: str, data_url: str, timeout: int) -> str:
    p = PROVIDERS[provider]
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}],
        "max_tokens": 1200,
        "temperature": 0.1,
    }).encode()
    req = urllib.request.Request(p["url"], data=body, headers={
        "Authorization": f"Bearer {_key(provider)}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    resp = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    return resp["choices"][0]["message"]["content"]


def _parse(raw: str) -> dict:
    """Models wrap JSON in fences or prose often enough to be worth handling."""
    t = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {"text": t, "kind": "other", "symbols": [], "numbers": {},
            "language": None, "summary": None, "_unparsed": True}


def _payload(path: Path, max_side: int = 1280, quality: int = 82) -> tuple:
    """Downscale before upload. A raw 150 KB screenshot base64s to ~200 KB and both
    vision models time out on it; 1280px/JPEG-82 keeps every digit legible while
    cutting the payload roughly tenfold. Falls back to the original bytes if Pillow
    is unavailable or the file is not a decodable image."""
    raw = Path(path).read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        if max(img.size) > max_side:
            ratio = max_side / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)),
                             Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return raw, mime


def verify_numbers(out: dict) -> dict:
    """Flag structured numbers that do not appear in the transcription.

    A VLM will occasionally emit a plausible number that is not in the image -- the
    11b model read a GMP of 45 as 46 in testing. For IPO and price content that is
    the difference between reporting and misinforming, so anything unconfirmed is
    marked rather than silently trusted. Callers should treat `numbers_unverified`
    as "do not publish this figure".
    """
    text = re.sub(r"[,\s]", "", (out.get("text") or ""))
    bad = []
    for field, val in (out.get("numbers") or {}).items():
        if val in (None, "", []):
            continue
        probe = re.sub(r"[,\s]", "", str(val))
        digits = re.sub(r"\D", "", probe)
        if digits and digits not in re.sub(r"\D", "", text) and probe not in text:
            bad.append(field)
    out["numbers_unverified"] = bad
    out["numbers_ok"] = not bad
    return out


def read_image(path: Path, provider: str | None = None, model: str | None = None,
               timeout: int = 240) -> dict:
    """OCR one image. Tries the chosen model, then the provider's fallbacks."""
    dp, dm = defaults()
    provider = provider or dp
    model = model or dm
    raw_bytes, mime = _payload(path)
    data_url = f"data:{mime};base64,{base64.b64encode(raw_bytes).decode()}"

    p = PROVIDERS[provider]
    tried, last = [], None
    for m in [model or p["default_model"], *p.get("fallbacks", [])]:
        if m in tried:
            continue
        tried.append(m)
        try:
            out = verify_numbers(_parse(_call(provider, m, data_url, timeout)))
            out["_model"] = m
            return out
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} on {m}: {e.read()[:180].decode(errors='replace')}"
        except Exception as e:
            last = f"{type(e).__name__} on {m}: {e}"
    raise RuntimeError(last or "all models failed")
