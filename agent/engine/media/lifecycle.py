"""Media is deliberately short-lived.

In trading every day is a new day: yesterday's chart screenshot has no value, but
the numbers that were in it do. So the cycle is

    fetch today's images  ->  OCR  ->  keep the text forever  ->  delete the bytes

Which keeps the disk flat regardless of how long this runs, and works the same on
an 8 GB EC2 or a runner that is wiped every six hours. `ocr_text` / `ocr_json` on
content.media survive; `storage_ref` is cleared and `deleted_at` stamped.

Nothing is ever deleted before it has been read: prune() refuses to touch a row
that has no ocr_at, unless --force says otherwise.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine import config, db
from engine.content import ocr
from engine.sources.telegram.ingest import media_kind as ocr_kind


def pending(limit: int = 100, vertical: str | None = None) -> list:
    """Downloaded images that have not been read yet."""
    q = """SELECT m.id, m.storage_ref, m.kind, i.posted_at, c.title AS channel, c.folder
           FROM content.media m
           JOIN content.items i ON i.id = m.item_id
           JOIN content.source_channels c ON c.id = i.channel_id
           WHERE m.ocr_at IS NULL AND m.deleted_at IS NULL
             AND m.storage = 'local' AND m.storage_ref IS NOT NULL
             AND m.kind IN ('photo', 'document')"""
    params = []
    if vertical:
        q += " AND c.folder = %s"
        params.append(vertical)
    q += " ORDER BY i.posted_at DESC NULLS LAST LIMIT %s"
    params.append(limit)
    return db.fetch_all(q, tuple(params))


def _find(ref: str) -> Path | None:
    """storage_ref is a bare filename; media lives per-channel under the vertical."""
    for base in (config.ROOT / "verticals", config.EXPORTS):
        if base.is_dir():
            for p in base.rglob(ref):
                return p
    return None


def run_ocr(limit: int = 100, vertical: str | None = None, provider: str = "nvidia",
            model: str | None = None) -> dict:
    rows = pending(limit, vertical)
    if not rows:
        print("nothing waiting for OCR.")
        return {"read": 0, "failed": 0}

    print(f"reading {len(rows)} image(s) with {provider}"
          + (f" / {model}" if model else " (default model)"))
    ok = bad = 0
    for i, r in enumerate(rows, 1):
        path = _find(r["storage_ref"])
        if not path:
            db.execute("UPDATE content.media SET ocr_error = %s, ocr_at = now() WHERE id = %s",
                       ("file missing on disk", r["id"]))
            bad += 1
            continue
        try:
            out = ocr.read_image(path, provider, model)
            db.execute(
                """UPDATE content.media
                   SET ocr_text = %s, ocr_json = %s, ocr_model = %s, ocr_at = now(),
                       ocr_error = NULL
                   WHERE id = %s""",
                (out.get("text"), db.psycopg2.extras.Json(out), out.get("_model"), r["id"]))
            ok += 1
            kind = out.get("kind", "?")
            syms = ", ".join(out.get("symbols") or [])[:40]
            print(f"  [{i}/{len(rows)}] {r['channel'][:26]:28} {kind:<11} {syms}")
        except Exception as e:
            db.execute("UPDATE content.media SET ocr_error = %s, ocr_at = now() WHERE id = %s",
                       (f"{type(e).__name__}: {e}"[:400], r["id"]))
            bad += 1
            print(f"  [{i}/{len(rows)}] {r['channel'][:26]:28} FAILED {type(e).__name__}")
    print(f"\nread {ok}, failed {bad}")
    return {"read": ok, "failed": bad}


def prune(keep_days: int = 1, force: bool = False, dry_run: bool = False) -> dict:
    """Delete local image files older than keep_days. OCR text is kept.

    keep_days=1 means "today's images stay, yesterday's go" -- the intended cycle.
    A row that has not been OCR'd is left alone unless force=True, so nothing is
    thrown away before its content has been captured.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    q = """SELECT m.id, m.storage_ref, m.bytes, m.ocr_at, i.posted_at
           FROM content.media m JOIN content.items i ON i.id = m.item_id
           WHERE m.storage = 'local' AND m.storage_ref IS NOT NULL
             AND m.deleted_at IS NULL AND i.posted_at < %s"""
    if not force:
        q += " AND m.ocr_at IS NOT NULL"
    rows = db.fetch_all(q, (cutoff,))
    if not rows:
        print(f"nothing to prune (cutoff {cutoff:%Y-%m-%d %H:%M} UTC)"
              + ("" if force else "; un-OCR'd files are kept — use --force to drop them anyway"))
        return {"deleted": 0, "freed": 0}

    freed = gone = 0
    for r in rows:
        p = _find(r["storage_ref"])
        if p and p.is_file():
            freed += p.stat().st_size
            if not dry_run:
                p.unlink()
        if not dry_run:
            db.execute(
                "UPDATE content.media SET deleted_at = now(), storage = 'deleted', "
                "storage_ref = NULL WHERE id = %s", (r["id"],))
        gone += 1

    verb = "would delete" if dry_run else "deleted"
    print(f"{verb} {gone} file(s), {float(freed) / 1e6:.1f} MB freed — OCR text kept")
    if not force:
        left = db.fetch_one(
            """SELECT count(*) n FROM content.media m JOIN content.items i ON i.id = m.item_id
               WHERE m.storage='local' AND m.deleted_at IS NULL AND m.ocr_at IS NULL
                 AND i.posted_at < %s""", (cutoff,))["n"]
        if left:
            print(f"{left} old file(s) kept because they have not been OCR'd yet")
    return {"deleted": gone, "freed": freed}


def status() -> None:
    r = db.fetch_one("""
        SELECT count(*) total,
               count(*) FILTER (WHERE storage='local' AND deleted_at IS NULL) on_disk,
               count(*) FILTER (WHERE ocr_at IS NOT NULL AND ocr_error IS NULL) read,
               count(*) FILTER (WHERE ocr_error IS NOT NULL) failed,
               count(*) FILTER (WHERE deleted_at IS NOT NULL) pruned,
               coalesce(sum(bytes) FILTER (WHERE storage='local' AND deleted_at IS NULL),0) live_bytes
        FROM content.media""")
    print(f"  media rows   {r['total']:>8}")
    print(f"  on disk      {r['on_disk']:>8}   ({float(r['live_bytes'])/1e6:.1f} MB)")
    print(f"  OCR done     {r['read']:>8}")
    print(f"  OCR failed   {r['failed']:>8}")
    print(f"  pruned       {r['pruned']:>8}   (text retained)")


async def fetch_recent(vertical: str, days: int = 1, limit: int = 200,
                       account: str | None = None) -> dict:
    """Download the actual image bytes for recent items that have none.

    Ingest records that a message HAD media without necessarily pulling it, and the
    archive resume point moves past a message once its text is stored. So getting
    images for "the last N days" needs a targeted pass: find media rows with no file,
    re-open just those messages by id, and download. Bounded by `days` on purpose --
    old images are worthless here and would only be pruned tomorrow anyway.
    """
    from datetime import datetime, timedelta, timezone as _tz
    from engine import verticals
    from engine.sources.telegram import client as tgclient

    cfg = verticals.load(vertical)
    account = account or cfg.get("source", {}).get("account")
    cutoff = datetime.now(_tz.utc) - timedelta(days=days)
    folder = cfg.get("display_name") or vertical

    # Driven off items.has_media, not off an existing media row: the archive writes
    # the flag but does not create a media row until bytes actually arrive, so this
    # has to cover both "row exists but empty" and "no row yet".
    rows = db.fetch_all(
        """SELECT i.id AS item_id, m.id AS media_id, i.message_id,
                  c.id AS channel_id, c.title, c.username
           FROM content.items i
           JOIN content.source_channels c ON c.id = i.channel_id
           LEFT JOIN content.media m ON m.item_id = i.id
           WHERE c.folder = %s AND i.posted_at >= %s AND i.has_media
             AND (m.id IS NULL OR (m.storage_ref IS NULL AND m.deleted_at IS NULL))
           ORDER BY i.posted_at DESC LIMIT %s""",
        (folder, cutoff, limit))
    if not rows:
        print(f"no un-fetched media in the last {days} day(s) for '{vertical}'.")
        return {"fetched": 0}

    print(f"{len(rows)} image(s) to fetch from the last {days} day(s)")
    base = config.ROOT / "verticals" / vertical / "messages" / "media"
    got = 0
    async with tgclient.connected(account) as (client, _acct):
        by_chan = {}
        for r in rows:
            by_chan.setdefault(r["channel_id"], []).append(r)
        for cid, group in by_chan.items():
            try:
                entity = await client.get_entity(cid)
            except Exception as e:
                print(f"  ! {group[0]['title'][:34]}: {type(e).__name__}")
                continue
            ids = [g["message_id"] for g in group]
            out = base / (group[0]["username"] or str(cid))
            out.mkdir(parents=True, exist_ok=True)
            msgs = await client.get_messages(entity, ids=ids)
            for g, msg in zip(group, msgs):
                if not msg or not msg.media:
                    continue
                try:
                    path = await client.download_media(msg, file=str(out))
                    if path:
                        p = Path(path)
                        kind = ocr_kind(msg)
                        if g["media_id"]:
                            db.execute(
                                "UPDATE content.media SET storage='local', storage_ref=%s,"
                                " bytes=%s, kind=%s, downloaded_at=now() WHERE id=%s",
                                (p.name, p.stat().st_size, kind, g["media_id"]))
                        else:
                            db.execute(
                                "INSERT INTO content.media (item_id, kind, storage,"
                                " storage_ref, bytes, downloaded_at)"
                                " VALUES (%s,%s,'local',%s,%s,now())",
                                (g["item_id"], kind, p.name, p.stat().st_size))
                        got += 1
                except Exception as e:
                    print(f"    {g['message_id']}: {type(e).__name__}")
            print(f"  {group[0]['title'][:40]:42} {got} fetched so far")
    print(f"\nfetched {got} file(s)")
    return {"fetched": got}


def roll_media(vertical: str, keep_days: int = 1, dry_run: bool = False) -> dict:
    """Keep only the latest day of media per channel; drop everything older.

    In trading yesterday's screenshot is worthless, so each channel's media folder
    is a rolling window rather than an archive: today's files stay, anything older
    goes. A channel that posted no media today ends up with an EMPTY folder -- the
    folder is still created, so "no media today" is visible rather than ambiguous.

    Disk therefore stays flat forever. The text and any OCR output are untouched:
    only the bytes are transient.
    """
    from engine import archive, tiers

    man = archive.read_manifest(vertical)
    cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
    kept = dropped = freed = empty = 0

    for cid, c in man.get("channels", {}).items():
        d = tiers.media_dir_for(vertical, int(cid), c.get("title") or "", c.get("username"))
        if not dry_run:
            d.mkdir(parents=True, exist_ok=True)
        if not d.is_dir():
            continue
        files = [f for f in d.iterdir() if f.is_file()]
        for f in files:
            if f.stat().st_mtime < cutoff:
                freed += f.stat().st_size
                dropped += 1
                if not dry_run:
                    f.unlink()
            else:
                kept += 1
        if d.is_dir() and not any(f.is_file() for f in d.iterdir()):
            empty += 1          # posted nothing today — folder stays, visibly empty

    verb = "would drop" if dry_run else "dropped"
    print(f"media roll ({keep_days}d window): kept {kept}, {verb} {dropped} "
          f"({freed/1e6:.1f} MB), {empty} channel folder(s) now empty")
    return {"kept": kept, "dropped": dropped, "freed": freed, "empty": empty}
