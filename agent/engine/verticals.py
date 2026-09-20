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


def load_remote(name: str) -> dict | None:
    """The definition as last synced into Supabase, or None.

    This is what a runner reads. The runner repo is PUBLIC — that is what buys the free
    Actions minutes the whole design rests on — so a vertical whose channel list should
    not be world-readable ships its CODE to the repo and keeps its `vertical.json` and
    `tiers.json` here instead. Supabase is private; git history is forever.
    """
    try:
        r = db.fetch_one("SELECT config FROM content.verticals WHERE name = %s", (name,))
    except Exception:
        return None
    if not r or not r.get("config"):
        return None
    cfg = r["config"]
    return json.loads(cfg) if isinstance(cfg, str) else cfg


def load_any(name: str) -> dict:
    """Disk first, then Supabase. Raises only when neither has it.

    Disk wins because it is the hand-edited source of truth and a developer editing
    `vertical.json` must see the edit take effect without a sync. On a runner there is no
    file, so the database answers.
    """
    path = DIR / name / "vertical.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    cfg = load_remote(name)
    if cfg is None:
        raise SystemExit(f"vertical '{name}' is neither on disk ({path}) nor in "
                         f"content.verticals. Run: run.py vertical sync {name}")
    return cfg


def available_any() -> list:
    """Every vertical this process can see, from disk and from Supabase."""
    names = set(available())
    try:
        for r in db.fetch_all("SELECT name FROM content.verticals WHERE is_enabled"):
            names.add(r["name"])
    except Exception:
        pass
    return sorted(names)


def sync(name: str) -> dict:
    """Push the definition into Supabase and tag every channel with the vertical.

    `tiers.json` is carried inside the same blob under `_tiers`. The watcher needs the
    tier placements to know which channels to watch, and shipping that file to a public
    repo would publish the channel list it is supposed to keep out of there — so it
    travels with the config rather than alongside it.
    """
    from engine import tiers as _tiers
    cfg = load(name)
    tp = _tiers.path(name)
    if tp.is_file():
        cfg = {**cfg, "_tiers": json.loads(tp.read_text(encoding="utf-8"))}
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
