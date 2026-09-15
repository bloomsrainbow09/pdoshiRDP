"""Realtime Telegram watcher. Captures reliably; classifies nothing.

Design decisions, each forced by a constraint rather than chosen for elegance:

**Push, not polling.** Telethon's `events.NewMessage` delivers in well under a second.
Polling 48 channels every second would be ~4 million API calls a day and would get the
account rate-limited or banned. There is no version of "check every second" that is
better than the push handler.

**Gap replay on every start.** The runner is killed every 6 hours, so a gap between the
stored cursor and now is the normal case, not an exception. Every start closes it
before going live, or the handoff silently loses a window of messages.

**One watcher per account, enforced by a database lease.** Two clients sharing one
Telegram session can get the auth key REVOKED — that needs a human with a phone and
would permanently end the zero-human guarantee. The 6-hour handoff overlaps by ~5
minutes, which is exactly the hazard.

**The watcher makes no decisions.** It writes to `agent_events` and stops. Anything it
classified would be a decision made without the router's context, and a bug here would
lose messages rather than merely misroute them.
"""

import asyncio
import json
import os
import signal
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

AGENT = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT))
sys.path.insert(0, str(AGENT.parent))

from telethon import events, utils                      # noqa: E402
from telethon.errors import FloodWaitError              # noqa: E402

from core import active                      # noqa: E402
import db as adb                                        # noqa: E402
from engine import tiers, verticals                     # noqa: E402
from engine.sources.telegram import accounts, client as tgclient, ingest  # noqa: E402

WATCHED_TIERS = {"tier1", "tier2", "tier3"}   # `useless` is deliberately not watched
LEASE_SECONDS = 300
HEARTBEAT_SECONDS = 60
WORKER_ID = os.environ.get("WORKER_ID") or f"{os.name}-{uuid.uuid4().hex[:8]}"


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def watched_channels(vertical: str | None = None) -> dict:
    vertical = vertical or active.name()
    """{channel_id: {title, tier, category}} for every channel in a watched tier."""
    placements = tiers.load(vertical)["placements"]
    out = {}
    for cid, p in placements.items():
        if p.get("tier") in WATCHED_TIERS:
            out[int(cid)] = {"title": p.get("title") or cid,
                             "tier": p["tier"], "category": p.get("category", "")}
    return out


def capture(msg, meta: dict) -> dict | None:
    """Persist one message. Returns the new row, or None if already stored.

    Deliberately tolerant: a message that cannot be parsed is still recorded with
    whatever is available, because losing it is worse than storing it imperfectly.
    """
    urls = ingest.extract_urls(msg)
    text = msg.message or ""
    return adb.record_event(
        channel_id=meta["channel_id"], message_id=msg.id,
        channel_title=meta["title"], tier=meta["tier"],
        posted_at=msg.date.astimezone(timezone.utc) if msg.date else None,
        text=text, urls=urls,
        has_media=bool(msg.media), media_kind=ingest.media_kind(msg),
        content_hash=ingest.content_hash(text, urls),
        status="received",
    )


async def replay_gap(client, chans: dict, limit_per_channel: int = 500) -> int:
    """Close the gap between each channel's cursor and now, before going live.

    Bounded concurrency: 48 channels at once would trip Telegram's limits, and one slow
    channel must not block the others.
    """
    async def one(item):
        cid, meta = item
        cursor = adb.cursor_for(cid)
        got = 0
        try:
            entity = await client.get_entity(cid)
            async for msg in client.iter_messages(entity, min_id=cursor, reverse=True,
                                                  limit=limit_per_channel, wait_time=1):
                if capture(msg, {**meta, "channel_id": cid}):
                    got += 1
                adb.advance_cursor(cid, msg.id)
        except FloodWaitError as e:
            log(f"  ! flood wait {e.seconds}s on {meta['title'][:30]} — skipping to live")
        except Exception as e:
            log(f"  ! {meta['title'][:30]}: {type(e).__name__}: {str(e)[:70]}")
            adb.dead_letter("watcher", type(e).__name__, str(e), context={"channel_id": cid})
        return got

    sem = asyncio.Semaphore(4)

    async def guarded(item):
        async with sem:
            return await one(item)

    counts = await asyncio.gather(*(guarded(i) for i in chans.items()))
    return sum(counts)


async def run(vertical: str | None = None, account: str | None = None,
              duration_s: int | None = None) -> dict:
    adb.migrate()
    cfg = verticals.load(vertical)
    account = account or cfg.get("source", {}).get("account")
    chans = watched_channels(vertical)
    stats = {"replayed": 0, "live": 0, "duplicates": 0, "errors": 0}

    if not adb.acquire_lease(account, WORKER_ID, LEASE_SECONDS):
        log(f"another watcher holds the lease for '{account}' — refusing to start. "
            f"Two clients on one session risk the auth key being revoked.")
        return {**stats, "refused": True}
    log(f"lease acquired: worker {WORKER_ID}, account '{account}', "
        f"{len(chans)} channels in {sorted(WATCHED_TIERS)}")

    stop = asyncio.Event()

    async with tgclient.connected(account) as (client, row):
        # ---- 1. close the gap the 6-hour handoff left ----
        log("replaying gap since last cursor…")
        stats["replayed"] = await replay_gap(client, chans)
        log(f"gap closed: {stats['replayed']} message(s) captured")

        # ---- 2. go live ----
        @client.on(events.NewMessage(chats=list(chans)))
        async def on_new(event):
            try:
                cid = utils.get_peer_id(await event.get_chat())
                meta = chans.get(cid)
                if not meta:
                    return
                row = capture(event.message, {**meta, "channel_id": cid})
                adb.advance_cursor(cid, event.message.id)
                if row:
                    stats["live"] += 1
                    log(f"  + {meta['tier']:<6} {meta['title'][:30]:<32} "
                        f"msg {event.message.id} ({len(event.message.message or '')} ch)")
                else:
                    stats["duplicates"] += 1
            except Exception as e:
                stats["errors"] += 1
                adb.dead_letter("watcher", type(e).__name__, str(e),
                                context={"message_id": getattr(event.message, "id", None)})
                log(f"  ! handler error {type(e).__name__}: {str(e)[:80]}")

        async def beat():
            while not stop.is_set():
                await asyncio.sleep(HEARTBEAT_SECONDS)
                adb.heartbeat(account, WORKER_ID, LEASE_SECONDS)

        beater = asyncio.create_task(beat())
        if duration_s:
            log(f"live — running {duration_s}s")
            try:
                await asyncio.wait_for(stop.wait(), timeout=duration_s)
            except asyncio.TimeoutError:
                pass
        else:
            log("live — Ctrl-C to stop")
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    asyncio.get_running_loop().add_signal_handler(sig, stop.set)
                except NotImplementedError:
                    pass          # Windows
            await stop.wait()

        stop.set()
        beater.cancel()

    adb.release_lease(account, WORKER_ID)
    log(f"stopped — replayed {stats['replayed']}, live {stats['live']}, "
        f"dupes {stats['duplicates']}, errors {stats['errors']}")
    return stats


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--vertical", default=None,
                    help=f"which vertical to watch (default: {active.name()})")
    ap.add_argument("--account")
    ap.add_argument("--seconds", type=int, help="run for N seconds then exit (tests)")
    a = ap.parse_args()
    out = asyncio.run(run(a.vertical, a.account, a.seconds))
    print(json.dumps(out, indent=2))
