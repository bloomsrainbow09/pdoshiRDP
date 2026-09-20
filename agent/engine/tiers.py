"""Where each channel's messages live on disk.

Layout under verticals/<name>/messages/ :

    tier1/<category>/<slug>__<id>.jsonl     the channels worth using
    tier1/media/<slug>/                     ONLY the most recent day's media
    tier1/TIER1.md                          why each channel is here
    tier2/… tier3/…                         same shape
    newly-added/                            everything not yet categorised

Placement is data, in verticals/<name>/tiers.json, keyed by channel id. A channel
the pipeline has never seen lands in `newly-added` and stays there until a human
moves it -- discovery must never silently promote something into tier1.

Ids are the key, not names, because these channels rename themselves constantly.
"""

import json
import re

from engine import verticals

DEFAULT_TIER = "newly-added"

TIER_LABELS = {
    "tier1": "Tier 1 — primary sources, verified accurate",
    "tier2": "Tier 2 — useful with caveats",
    "tier3": "Tier 3 — language/regional and low-signal",
    "newly-added": "Newly added — not yet categorised",
    "useless": "Useless — spam, unverifiable win-claims, or pure promotion",
}


def path(name: str):
    return verticals.DIR / name / "tiers.json"


def load(name: str) -> dict:
    """Placements from disk, else from the synced config in Supabase.

    A runner has no `tiers.json` for a vertical whose channel list is deliberately kept
    out of the public repo, so `verticals.sync()` carries it inside the config blob under
    `_tiers` and this reads it back. Returning empty placements instead would be worse
    than an error: the watcher would start, find nothing in a watched tier, and quietly
    watch zero channels.
    """
    p = path(name)
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    cfg = verticals.load_remote(name) or {}
    remote = cfg.get("_tiers")
    if remote:
        return remote
    return {"placements": {}, "labels": TIER_LABELS}


def save(name: str, data: dict) -> None:
    data.setdefault("labels", TIER_LABELS)
    path(name).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def place(name: str, cid: int) -> tuple:
    """(tier, category) for a channel. Unknown channels go to newly-added."""
    p = load(name)["placements"].get(str(cid))
    if not p:
        return DEFAULT_TIER, ""
    return p.get("tier", DEFAULT_TIER), p.get("category", "")


def set_place(name: str, cid: int, tier: str, category: str = "",
              note: str = "", title: str = "") -> None:
    data = load(name)
    cur = data["placements"].get(str(cid), {})
    data["placements"][str(cid)] = {
        "tier": tier, "category": category,
        "note": note or cur.get("note", ""),
        "title": title or cur.get("title", ""),
    }
    save(name, data)


def slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(s)).strip("-")
    return re.sub(r"-+", "-", s)[:60] or "channel"


def dir_for(name: str, cid: int):
    """Folder that this channel's .jsonl belongs in."""
    tier, cat = place(name, cid)
    base = verticals.DIR / name / "messages" / tier
    return base / cat if cat else base


def media_dir_for(name: str, cid: int, title: str, username: str | None):
    """tier<N>/media/<channel-slug>/ — a rolling one-day window, not an archive."""
    tier, _ = place(name, cid)
    return (verticals.DIR / name / "messages" / tier / "media"
            / slug(username or title or str(cid)))


def by_tier(name: str) -> dict:
    """{tier: {category: [placement dicts]}} for reporting and doc generation."""
    out = {}
    for cid, p in load(name)["placements"].items():
        t = p.get("tier", DEFAULT_TIER)
        c = p.get("category", "")
        out.setdefault(t, {}).setdefault(c, []).append({**p, "id": int(cid)})
    return out


def organize(name: str, dry_run: bool = False) -> dict:
    """Move every channel's .jsonl into its tier/category folder.

    Idempotent: a file already in the right place is left alone. Run it after
    editing tiers.json and the layout catches up; the downloader then keeps writing
    to the new location because the manifest stores the path, not just a filename.
    """
    from engine import archive

    man = archive.read_manifest(name)
    root = verticals.DIR / name / "messages"
    moved = same = missing = 0

    for cid, c in man.get("channels", {}).items():
        rel = c.get("file") or ""
        src = root / rel
        if not src.is_file():
            missing += 1
            continue
        tier, cat = place(name, int(cid))
        dst = (root / tier / cat if cat else root / tier) / src.name
        if src.resolve() == dst.resolve():
            same += 1
            continue
        print(f"  {src.name[:46]:48} {rel.rsplit('/', 1)[0] or '.'} -> {tier}/{cat}".rstrip("/"))
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dst)
            c["file"] = str(dst.relative_to(root)).replace("\\", "/")
            c["tier"], c["category"] = tier, cat
        moved += 1

    if not dry_run:
        archive.write_manifest(name, man)
        # every tier gets its media/ root so the structure is visible even when empty
        for t in load(name).get("labels", TIER_LABELS):
            (root / t / "media").mkdir(parents=True, exist_ok=True)
        for stray in root.glob("*.jsonl"):        # anything never placed
            dst = root / DEFAULT_TIER / stray.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            stray.replace(dst)
            moved += 1

    print(f"\n{'would move' if dry_run else 'moved'} {moved}, {same} already in place"
          + (f", {missing} file(s) missing" if missing else ""))
    return {"moved": moved, "same": same, "missing": missing}
