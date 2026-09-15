"""Verticals: a niche defined as data, not code.

verticals/<name>/vertical.json is the hand-edited source of truth. sync() mirrors
it into content.verticals and tags the channels, so a runner reads config from the
database and never needs the folder to decide anything.
"""

import json

from engine import config, db

DIR = config.ROOT / "verticals"


def available() -> list:
    if not DIR.is_dir():
        return []
    return sorted(p.name for p in DIR.iterdir()
                  if p.is_dir() and (p / "vertical.json").is_file())


def load(name: str) -> dict:
    path = DIR / name / "vertical.json"
    if not path.is_file():
        known = ", ".join(available()) or "(none)"
        raise SystemExit(f"no vertical '{name}' at {path}. Known: {known}")
    return json.loads(path.read_text(encoding="utf-8"))


def prompt(name: str, which: str) -> str | None:
    p = DIR / name / "prompts" / f"{which}.md"
    return p.read_text(encoding="utf-8") if p.is_file() else None


def channels(cfg: dict, enabled_only: bool = True) -> list:
    chans = cfg.get("source", {}).get("channels", [])
    return [c for c in chans if c.get("enabled", True)] if enabled_only else chans


def sync(name: str) -> dict:
    """Push the definition into Supabase and tag every channel with the vertical."""
    cfg = load(name)
    db.upsert("content.verticals", "name", name,
              display_name=cfg.get("display_name") or name,
              is_enabled=cfg.get("enabled", True),
              config=json.dumps(cfg))
    db.execute("UPDATE content.verticals SET synced_at = now() WHERE name = %s", (name,))

    tagged = 0
    for c in channels(cfg):
        tagged += db.execute(
            "UPDATE content.source_channels SET folder = %s, updated_at = now() WHERE id = %s",
            (cfg.get("display_name") or name, c["id"]))
    return {"vertical": name, "channels": len(channels(cfg)), "tagged_existing": tagged}
