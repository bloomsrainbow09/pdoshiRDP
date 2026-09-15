"""Pull messages from channels into content.items.

Resume state lives in content.source_channels.last_message_id, not a local file,
so a wiped runner picks up exactly where the last one stopped. (channel_id,
message_id) is UNIQUE, so a re-run can never duplicate.
"""

import hashlib
from pathlib import Path
from datetime import datetime, timezone

from telethon import utils
from telethon.errors import ChannelPrivateError, FloodWaitError, UsernameNotOccupiedError
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

from engine import db
from engine.sources.telegram import browse, client as tgclient
import asyncio


def extract_urls(msg) -> list:
    """Bare URLs plus the targets behind hyperlinked text.

    get_entities_text() rather than slicing msg.message by entity offsets: Telegram
    counts offsets in UTF-16 code units, so an emoji earlier in the text shifts a
    naive Python slice and you capture the wrong substring.
    """
    urls = []
    try:
        pairs = msg.get_entities_text()
    except (AttributeError, TypeError):
        return urls
    for ent, text in pairs:
        if isinstance(ent, MessageEntityTextUrl):
            urls.append(ent.url)
        elif isinstance(ent, MessageEntityUrl):
            urls.append(text)
    return urls


def content_hash(text: str, urls: list) -> str:
    """Identifies the same content forwarded across several channels."""
    basis = (text or "").strip().lower() + "|" + "|".join(sorted(urls or []))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest() if basis.strip("|") else None


def media_kind(msg) -> str | None:
    if not msg.media:
        return None
    name = type(msg.media).__name__
    if "Photo" in name:
        return "photo"
    doc = getattr(msg, "document", None)
    if doc:
        mime = getattr(doc, "mime_type", "") or ""
        if mime.startswith("video"):
            return "video"
        if mime.startswith("audio"):
            return "audio"
        return "document"
    return name


async def register_channel(client, target, account_label: str, folder: str | None = None) -> dict:
    """Ensure a content.source_channels row exists; returns it."""
    entity = await client.get_entity(target)
    cid = utils.get_peer_id(entity)
    db.upsert(
        "content.source_channels", "id", cid,
        account_label=account_label,
        title=getattr(entity, "title", None) or getattr(entity, "username", None) or str(cid),
        username=getattr(entity, "username", None),
        kind=browse.kind_of(entity),
        folder=folder,
    )
    row = db.fetch_one("SELECT * FROM content.source_channels WHERE id = %s", (cid,))
    row["_entity"] = entity
    return row


ITEM_SQL = """INSERT INTO content.items
    (channel_id, message_id, posted_at, text, urls, views, forwards, replies,
     grouped_id, reply_to, has_media, content_hash)
  VALUES %s
  ON CONFLICT (channel_id, message_id) DO NOTHING
  RETURNING id, message_id"""

MEDIA_SQL = """INSERT INTO content.media
    (item_id, kind, storage, storage_ref, bytes, downloaded_at) VALUES %s"""

BATCH = 200


async def ingest_channel(client, chan: dict, limit=None, since=None, full=False,
                         media_dir=None) -> dict:
    """Fetch new messages for one channel into content.items.

    Batched: a round trip to Supabase costs ~200ms, so inserting one message at a
    time would take minutes for a busy channel. Messages already stored are filtered
    locally against one up-front query, so a re-run also never re-downloads media.
    """
    entity = chan["_entity"]
    start = 0 if full else int(chan.get("last_message_id") or 0)
    highest, new, skipped = start, 0, 0

    known = {r["message_id"] for r in db.fetch_all(
        "SELECT message_id FROM content.items WHERE channel_id = %s", (chan["id"],))}

    print(f"\n{chan['title']}  (id={chan['id']})")
    print(f"  resuming after message {start}" if start else "  full history",
          f"| {len(known)} already stored" if known else "")

    items, media = [], {}

    def flush():
        nonlocal items, media
        if not items:
            return
        got = db.insert_many(ITEM_SQL, items, fetch=True)
        ids = {mid: iid for iid, mid in got}
        rows = [(ids[mid],) + m for mid, mlist in media.items() if mid in ids for m in mlist]
        db.insert_many(MEDIA_SQL, rows)
        db.execute("UPDATE content.source_channels SET last_message_id = %s WHERE id = %s",
                   (highest, chan["id"]))
        items, media = [], {}

    while True:  # re-entered only to resume after a FloodWait
        try:
            async for msg in client.iter_messages(entity, limit=limit, min_id=highest,
                                                  reverse=True, wait_time=1):
                highest = max(highest, msg.id)
                if msg.id in known:
                    continue
                if since and msg.date and msg.date < since:
                    skipped += 1
                    continue

                urls = extract_urls(msg)
                items.append((
                    chan["id"], msg.id,
                    msg.date.astimezone(timezone.utc) if msg.date else None,
                    msg.message or "", urls,
                    getattr(msg, "views", None), getattr(msg, "forwards", None),
                    getattr(msg.replies, "replies", None) if getattr(msg, "replies", None) else None,
                    getattr(msg, "grouped_id", None), getattr(msg, "reply_to_msg_id", None),
                    bool(msg.media), content_hash(msg.message, urls)))
                new += 1

                if msg.media:
                    ref, nbytes = None, None
                    if media_dir:
                        try:
                            media_dir.mkdir(parents=True, exist_ok=True)
                            path = await client.download_media(msg, file=str(media_dir))
                            if path:
                                ref, nbytes = Path(path).name, Path(path).stat().st_size
                        except Exception as e:  # one bad file must not end the run
                            print(f"    media {msg.id} failed: {type(e).__name__}")
                    media.setdefault(msg.id, []).append(
                        (media_kind(msg), "local" if ref else "none", ref, nbytes,
                         datetime.now(timezone.utc) if ref else None))

                if len(items) >= BATCH:
                    flush()
                    print(f"    {new} new (at message {msg.id})")
            break
        except FloodWaitError as e:
            flush()  # never lose a partial batch to a long sleep
            print(f"    rate-limited, sleeping {e.seconds}s then resuming at {highest}")
            await asyncio.sleep(e.seconds + 2)
    flush()

    db.execute(
        "UPDATE content.source_channels SET last_message_id = %s, last_synced_at = now() WHERE id = %s",
        (highest, chan["id"]))
    total = db.fetch_one("SELECT count(*) n FROM content.items WHERE channel_id = %s",
                         (chan["id"],))["n"]
    note = f", {skipped} older than --since" if skipped else ""
    print(f"  +{new} new{note} — {total} stored")
    return {"channel": chan["title"], "new": new, "total": total}


async def run(label, targets, limit=None, since=None, full=False, folder=None, media_dir=None):
    async with tgclient.connected(label) as (client, row):
        print(f"reading as '{row['label']}'")
        results = []
        for t in targets:
            try:
                chan = await register_channel(client, t, row["label"], folder)
            except (UsernameNotOccupiedError, ValueError):
                print(f"  ! {t}: no such channel/username")
                continue
            except ChannelPrivateError:
                print(f"  ! {t}: private — the account must be a member")
                continue
            results.append(await ingest_channel(client, chan, limit, since, full, media_dir))
        return results
