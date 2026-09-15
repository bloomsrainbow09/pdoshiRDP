"""Download every message of every channel in a vertical, to files, incrementally.

The rules this implements:

  * The channel list is read LIVE from the Telegram chat folder on every run, so a
    channel added to the folder tomorrow is picked up with no config edit.
  * The FILE is the resume authority. No file -> download the whole history, A to Z.
    File present -> start after the highest message id already in it.
  * Therefore a newly added channel downloads everything while the others only
    collect what arrived since last time, in the same run.
  * Nothing is ever written twice: ids already in the file are skipped, so an
    interrupted run just resumes and a re-run is a no-op.

Files are append-only JSONL, one message per line, under
verticals/<name>/messages/. Rows also go to content.items so the database pipeline
sees the same data, but the file — not the database — decides where to resume, so
the archive stays correct even on a machine that has never run this before.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from telethon import utils
from telethon.errors import ChannelPrivateError, FloodWaitError, UsernameNotOccupiedError
import asyncio

from engine import db, tiers, verticals
from engine.sources.telegram import browse, client as tgclient, ingest

FLUSH_EVERY = 200


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(s)).strip("-")
    return re.sub(r"-+", "-", s)[:60] or "channel"


def vdir(name: str) -> Path:
    return verticals.DIR / name / "messages"


def _manifest_path(name: str) -> Path:
    return vdir(name) / "_manifest.json"


def read_manifest(name: str) -> dict:
    p = _manifest_path(name)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"channels": {}}


def write_manifest(name: str, man: dict) -> None:
    man["updated_at"] = datetime.now(timezone.utc).isoformat()
    p = _manifest_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(man, indent=1, ensure_ascii=False), encoding="utf-8")


def channel_file(name: str, man: dict, cid: int, title: str, username: str | None) -> Path:
    """Where this channel's messages go: tier/category folder, stable filename.

    The manifest stores a path RELATIVE to messages/, so re-tiering a channel moves
    its file without the next run treating it as new. A renamed channel keeps its
    existing filename, so a rename never causes a re-download either.
    """
    fname = f"{_slug(username or title)}__{cid}.jsonl"
    entry = man["channels"].get(str(cid))
    if entry and entry.get("file"):
        existing = vdir(name) / entry["file"]
        if existing.is_file():
            fname = existing.name
            wanted = tiers.dir_for(name, cid) / fname
            if existing.resolve() != wanted.resolve():
                # placement changed since last run -- carry the history across
                wanted.parent.mkdir(parents=True, exist_ok=True)
                existing.replace(wanted)
                print(f"    moved to {wanted.parent.relative_to(vdir(name))}/")
            return wanted
    return tiers.dir_for(name, cid) / fname


def scan_file(path: Path) -> tuple:
    """(set of message ids already stored, highest id, line count).

    The file is the source of truth for resumption, so it is read rather than
    trusted from the manifest — a half-written run then self-heals.
    """
    if not path.is_file():
        return set(), 0, 0
    ids, hi, n = set(), 0, 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                mid = json.loads(line).get("message_id")
            except json.JSONDecodeError:
                continue          # tolerate a torn final line from a hard kill
            if mid is not None:
                ids.add(mid)
                hi = max(hi, mid)
                n += 1
    return ids, hi, n


def record(msg, chan: dict, media_files: list) -> dict:
    urls = ingest.extract_urls(msg)
    return {
        "channel_id": chan["id"],
        "channel": chan["title"],
        "channel_username": chan.get("username"),
        "message_id": msg.id,
        "date": msg.date.astimezone(timezone.utc).isoformat() if msg.date else None,
        "text": msg.message or "",
        "urls": urls,
        "views": getattr(msg, "views", None),
        "forwards": getattr(msg, "forwards", None),
        "replies": getattr(msg.replies, "replies", None) if getattr(msg, "replies", None) else None,
        "grouped_id": getattr(msg, "grouped_id", None),
        "reply_to": getattr(msg, "reply_to_msg_id", None),
        "media_type": ingest.media_kind(msg),
        "media_files": media_files,
        "content_hash": ingest.content_hash(msg.message, urls),
    }


async def download_channel(client, name, man, chan, limit=None, want_media=False) -> dict:
    """One channel: resume from its file, append only what is new."""
    path = channel_file(name, man, chan["id"], chan["title"], chan.get("username"))
    path.parent.mkdir(parents=True, exist_ok=True)
    seen, resume, had = scan_file(path)

    label = chan["title"][:44]
    print(f"\n  {label}")
    print(f"    {'new channel — full history' if not had else f'{had} stored, resuming after {resume}'}")

    media_dir = tiers.media_dir_for(name, chan["id"], chan["title"], chan.get("username"))
    entity = chan["_entity"]
    added, db_rows = 0, []

    fh = path.open("a", encoding="utf-8")
    try:
        while True:  # re-entered only to resume after a FloodWait
            try:
                async for msg in client.iter_messages(entity, limit=limit, min_id=resume,
                                                      reverse=True, wait_time=1):
                    resume = max(resume, msg.id)
                    if msg.id in seen:
                        continue

                    files = []
                    if want_media and msg.media:
                        try:
                            media_dir.mkdir(parents=True, exist_ok=True)
                            got = await client.download_media(msg, file=str(media_dir))
                            if got:
                                files.append(Path(got).name)
                        except Exception as e:
                            print(f"      media {msg.id}: {type(e).__name__}")

                    rec = record(msg, chan, files)
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    seen.add(msg.id)
                    added += 1
                    db_rows.append((
                        chan["id"], msg.id,
                        msg.date.astimezone(timezone.utc) if msg.date else None,
                        rec["text"], rec["urls"], rec["views"], rec["forwards"],
                        rec["replies"], rec["grouped_id"], rec["reply_to"],
                        bool(msg.media), rec["content_hash"]))

                    if added % FLUSH_EVERY == 0:
                        fh.flush()
                        db.insert_many(ingest.ITEM_SQL, db_rows); db_rows = []
                        print(f"      {added} new (at {msg.id})")
                break
            except FloodWaitError as e:
                fh.flush()
                db.insert_many(ingest.ITEM_SQL, db_rows); db_rows = []
                print(f"      rate-limited, sleeping {e.seconds}s then resuming at {resume}")
                await asyncio.sleep(e.seconds + 2)
    finally:
        fh.close()

    db.insert_many(ingest.ITEM_SQL, db_rows)
    db.execute("UPDATE content.source_channels SET last_message_id = GREATEST(last_message_id, %s),"
               " last_synced_at = now() WHERE id = %s", (resume, chan["id"]))

    man["channels"][str(chan["id"])] = {
        "title": chan["title"], "username": chan.get("username"),
        "file": str(path.relative_to(vdir(name))).replace("\\", "/"),
        "tier": tiers.place(name, chan["id"])[0],
        "category": tiers.place(name, chan["id"])[1],
        "messages": had + added, "last_message_id": resume,
        "last_run": datetime.now(timezone.utc).isoformat(), "in_folder": True,
    }
    write_manifest(name, man)
    print(f"    +{added} new — {had + added} total in {path.name}")
    return {"title": chan["title"], "new": added, "total": had + added}


async def download_until_stable(name: str, limit=None, want_media=False,
                                max_passes: int = 6) -> dict:
    """Keep downloading until a pass discovers no new channels.

    A single pass reads the Telegram folder once, at the start. On a long run that
    snapshot goes stale -- channels added while it is working are missed and would
    wait until the next run. This repeats until a pass finds nothing new, so
    "download everything in the folder" is actually true when it returns.
    """
    total = {"channels": 0, "new": 0, "total": 0, "added_channels": 0, "passes": 0}
    bar = "#" * 70
    for i in range(1, max_passes + 1):
        print(f"\n{bar}\n# pass {i}\n{bar}")
        r = await download(name, limit, want_media, None)
        total["passes"] = i
        total["new"] += r["new"]
        total["added_channels"] += r["added_channels"]
        total["channels"] = r["channels"]
        total["total"] = r["total"]
        if not r["added_channels"]:
            print(f"\npass {i} found no new channels — folder fully downloaded.")
            break
        print(f"\npass {i} discovered {r['added_channels']} new channel(s); checking again.")
    else:
        print(f"\nstopped after {max_passes} passes — channels are still being added.")
    return total


async def download(name: str, limit=None, want_media=False, only=None) -> dict:
    """Every channel currently in the vertical's Telegram folder."""
    cfg = verticals.load(name)
    src = cfg.get("source", {})
    folder_name = src.get("telegram_folder")
    if not folder_name:
        raise SystemExit(f"vertical '{name}' has no source.telegram_folder")

    man = read_manifest(name)
    known_before = set(man["channels"])

    async with tgclient.connected(src.get("account")) as (client, acct):
        print(f"account '{acct['label']}' · folder '{folder_name}'")
        found = await browse.folders(acct["label"], folder_name)
        if not found:
            raise SystemExit(f"no Telegram folder matching '{folder_name}'")
        _, live = found[0]

        # Retired by hand: still in the Telegram folder, but must not come back on
        # the next run just because discovery still sees them. Filtered FIRST so they
        # are not reported as newly discovered either.
        excluded = {int(x) for x in (src.get("exclude_ids") or [])}
        if excluded:
            skipped = [c for c in live if int(c["id"]) in excluded]
            live = [c for c in live if int(c["id"]) not in excluded]
            if skipped:
                print(f"{len(skipped)} channel(s) excluded by vertical.json: "
                      + ", ".join(c["name"][:24] for c in skipped))

        # Live folder is the authority: anything added there shows up here with no
        # config edit, and anything removed stops being fetched without losing its file.
        live_ids = {str(c["id"]) for c in live}
        added_now = [c for c in live if str(c["id"]) not in known_before]
        gone = known_before - live_ids
        print(f"{len(live)} channel(s) in the folder"
              + (f" · {len(added_now)} new since last run" if added_now else "")
              + (f" · {len(gone)} no longer in the folder (files kept)" if gone else ""))
        for c in added_now:
            print(f"    + {c['name'][:50]}")
        for cid in gone:
            man["channels"][cid]["in_folder"] = False

        results = []
        for c in live:
            if only and str(c["id"]) != str(only) and (c["username"] or "").lower() != str(only).lower().lstrip("@"):
                continue
            try:
                chan = await ingest.register_channel(client, c["id"], acct["label"],
                                                     cfg.get("display_name") or name)
            except (UsernameNotOccupiedError, ValueError, ChannelPrivateError) as e:
                print(f"\n  ! {c['name'][:44]}: {type(e).__name__}")
                continue
            results.append(await download_channel(client, name, man, chan, limit, want_media))

    # Refresh the cached channel list in vertical.json so the file mirrors reality.
    cfg["source"]["channels"] = [
        {"id": c["id"], "username": c["username"], "title": c["name"],
         "kind": c["kind"], "enabled": True} for c in live
    ]
    (verticals.DIR / name / "vertical.json").write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    write_manifest(name, man)
    return {"channels": len(results), "new": sum(r["new"] for r in results),
            "total": sum(r["total"] for r in results), "added_channels": len(added_now)}


DEAD_AFTER_DAYS = 60


def dead_channels(name: str, days: int = DEAD_AFTER_DAYS) -> list:
    """Channels in the vertical whose newest stored message is older than `days`.

    Judged from what is actually on disk, so it stays true even if the database is
    unavailable. A channel that has simply gone quiet for a few weeks is NOT dead --
    60 days is the threshold on purpose, because several channels here go silent for
    a month and come back.
    """
    man = read_manifest(name)
    out = []
    for cid, c in man.get("channels", {}).items():
        path = vdir(name) / c.get("file", "")
        newest = None
        if path.is_file():
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line).get("date")
                    except json.JSONDecodeError:
                        continue
                    if d:
                        dt = datetime.fromisoformat(d)
                        if newest is None or dt > newest:
                            newest = dt
        if newest is None:
            continue
        age = (datetime.now(timezone.utc) - newest).days
        if age >= days:
            out.append({"id": int(cid), "title": c.get("title"), "username": c.get("username"),
                        "file": c.get("file"), "messages": c.get("messages", 0),
                        "age_days": age, "last": newest})
    return sorted(out, key=lambda x: -x["age_days"])


async def reap(name: str, days: int = DEAD_AFTER_DAYS, apply: bool = False,
               leave: bool = False, leave_private: bool = False) -> dict:
    """Retire dead channels: delete local chat files, drop DB rows, exclude from
    discovery, and optionally leave the Telegram channel.

    Leaving is opt-in (`leave`) and, by default, skips channels with no public
    username: those are private or paid, and leaving one can mean losing access for
    good. Everything short of leaving is reversible -- a re-added channel just
    downloads again.
    """
    from engine.sources.telegram import client as tgclient

    dead = dead_channels(name, days)
    if not dead:
        print(f"no channel in '{name}' has been silent for {days}+ days.")
        return {"found": 0}

    print(f"{len(dead)} channel(s) silent for {days}+ days:\n")
    print(f"{'channel':<38}{'msgs':>9}{'silent':>9}  username")
    print("-" * 78)
    for d in dead:
        print(f"{(d['title'] or '?')[:36]:<38}{d['messages']:>9}{d['age_days']:>8}d  "
              f"{('@' + d['username']) if d['username'] else '(private — leave skipped)'}")

    if not apply:
        print(f"\nDRY RUN. Re-run with --yes to delete locally"
              f"{', --leave to also leave on Telegram' if not leave else ''}.")
        return {"found": len(dead), "applied": False}

    man = read_manifest(name)
    cfg = verticals.load(name)
    freed = left = 0

    if leave:
        targets = [d for d in dead if d["username"] or leave_private]
        skipped = [d for d in dead if not d["username"] and not leave_private]
        if targets:
            async with tgclient.connected(cfg.get("source", {}).get("account")) as (client, _):
                for d in targets:
                    try:
                        # Resolve to an entity first. delete_dialog(<raw id>) can
                        # no-op silently when the id is not in the session cache --
                        # it reported success while the channel stayed joined.
                        ent = await client.get_entity(d["id"])
                        await client.delete_dialog(ent)
                        left += 1
                        print(f"  left  {d['title'][:40]}", flush=True)
                    except Exception as e:
                        print(f"  !     {d['title'][:40]}: {type(e).__name__}: "
                              f"{str(e)[:80]}", flush=True)
        for d in skipped:
            print(f"  kept  {d['title'][:40]} (private/paid — use --leave-private to force)")

    for d in dead:
        f = vdir(name) / (d["file"] or "")
        if f.is_file():
            freed += f.stat().st_size
            f.unlink()
        md = vdir(name) / "media" / _slug(d["username"] or d["title"] or "")
        if md.is_dir():
            for x in md.rglob("*"):
                if x.is_file():
                    freed += x.stat().st_size
                    x.unlink()
            try:
                md.rmdir()
            except OSError:
                pass
        db.execute("DELETE FROM content.source_channels WHERE id = %s", (d["id"],))
        man["channels"].pop(str(d["id"]), None)

    write_manifest(name, man)
    ex = set(cfg.setdefault("source", {}).get("exclude_ids", [])) | {d["id"] for d in dead}
    cfg["source"]["exclude_ids"] = sorted(ex)
    cfg["source"]["channels"] = [c for c in cfg["source"]["channels"]
                                 if c["id"] not in {d["id"] for d in dead}]
    (verticals.DIR / name / "vertical.json").write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nretired {len(dead)} channel(s) · {freed/1e6:.1f} MB freed"
          + (f" · {left} left on Telegram" if leave else " · Telegram untouched"))
    return {"found": len(dead), "freed": freed, "left": left, "applied": True}
