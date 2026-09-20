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


def watched_channels(vertical=None, account: str | None = None) -> dict:
    """{channel_id: {title, tier, category, vertical, account}} for watched channels.

    `vertical` may be one name or several. Several is not a convenience — it is the only
    way to watch more than one niche on a shared Telegram account. A session's lease can
    be held by exactly one client (two clients on one session from two IPs is
    AuthKeyDuplicatedError, permanent, phone required to recover), so trading, movies and
    deals on `india` cannot each run a watcher. They share one, and every channel carries
    the name of the vertical that owns it so the drain can route it back.

    `account` filters to the channels reachable from one session. A vertical may span
    accounts — movies does — so this is per channel, falling back to the vertical's own
    `source.account` when a placement does not say.
    """
    names = ([vertical] if isinstance(vertical, str) else
             list(vertical) if vertical else [active.name()])
    out: dict = {}
    for name in names:
        try:
            placements = tiers.load(name)["placements"]
        except Exception as e:
            log(f"  ! vertical '{name}' has no usable tiers.json: {type(e).__name__}")
            continue
        try:
            default_account = (verticals.load_any(name).get("source") or {}).get("account")
        except Exception:
            default_account = None
        for cid, p in placements.items():
            if p.get("tier") not in WATCHED_TIERS:
                continue
            acct = p.get("account") or default_account
            if account and acct and acct != account:
                continue
            cid = int(cid)
            if cid in out:
                # One channel claimed by two verticals would be captured once and routed
                # to whichever loaded last — a silent, order-dependent misrouting. Refuse
                # rather than pick.
                log(f"  ! channel {cid} is claimed by both '{out[cid]['vertical']}' and "
                    f"'{name}'; keeping '{out[cid]['vertical']}'")
                continue
            out[cid] = {"title": p.get("title") or cid, "tier": p["tier"],
                        "category": p.get("category", ""), "vertical": name,
                        "account": acct}
    return out


def verticals_on(account: str) -> list:
    """Every enabled vertical that draws from `account`, by name.

    Read from `vertical.json` rather than hard-coded so adding a niche to a shared
    account is a config change, not a code change.
    """
    names = []
    # `available_any()` and not a directory scan: a runner has no folder for a vertical
    # whose channel list is kept out of the public repo, so those exist only in
    # content.verticals and a scan of verticals/ would miss exactly the ones that matter.
    for n in verticals.available_any():
        if n.startswith("_"):
            continue
        try:
            cfg = verticals.load_any(n)
        except Exception:
            continue
        if not cfg.get("enabled", True):
            continue
        # Deliberately via accounts_for() rather than re-deriving from the channel list:
        # that is the single place `source.default_accounts` is honoured, and duplicating
        # the rule here is how deals ended up narrowed to india for the harvest while the
        # watcher still picked up all 30 of its dormant usa channels.
        if account in accounts_for(n):
            names.append(n)
    return names


def accounts_for(name: str) -> list:
    """Every Telegram account a single vertical draws on.

    Usually one. `movies` is the case that forces this to be a list: its two most recent
    channels are on `india` and its three largest are on `usa`.
    """
    try:
        cfg = verticals.load_any(name)
    except Exception:
        return []
    src = cfg.get("source") or {}

    # `default_accounts` narrows a vertical that is CONFIGURED for several accounts to
    # the ones it should actually read. Deals lists 38 channels across both sessions but
    # is set to india only for now; without this the watcher would happily capture the
    # 30 dormant usa ones — 816 posts in a two-hour sample — while the harvest CLI, which
    # already honours the setting, read none of them. One switch, both paths.
    declared = [a for a in (src.get("default_accounts") or []) if a]
    if declared:
        return sorted(set(declared))

    out = {c.get("account") for c in (src.get("channels") or []) if c.get("account")}
    if src.get("account"):
        out.add(src["account"])
    return sorted(a for a in out if a)


def accounts_with_verticals() -> list:
    """Every account any enabled vertical draws on.

    One watcher can cover one account, so this is the list of watchers a supervisor
    needs to start. Holding two DIFFERENT sessions at once is fine — they are separate
    authorizations; it is two clients on ONE session that revokes an auth key.
    """
    out = set()
    for n in verticals.available_any():
        if n.startswith("_"):
            continue
        out.update(accounts_for(n))
    return sorted(out)


def file_meta(msg) -> dict | None:
    """Filename, size, dimensions and duration of an attached document, or None.

    `has_media` and `media_kind` say THAT a file arrived, never WHICH. For a vertical
    whose content is a human sentence that is enough — trading reads the text. For a
    file-centric one it is nothing at all: a movie channel posts
    `Enola.Holmes.3.2026.1080p.WEB-DL.mkv` with an empty caption, and an event recording
    only `media_kind='video'` has discarded the entire message.

    This is deliberately generic — filename, bytes, mime, w/h, duration are Telegram
    facts, not movie facts — and it lands in the existing `raw` jsonb rather than adding
    columns, so no vertical that does not care ever sees it.
    """
    doc = getattr(getattr(msg, "media", None), "document", None)
    if doc is None:
        return None
    name = dur = w = h = None
    for a in getattr(doc, "attributes", []) or []:
        cls = type(a).__name__
        if cls == "DocumentAttributeFilename":
            name = a.file_name
        elif cls == "DocumentAttributeVideo":
            dur, w, h = getattr(a, "duration", None), a.w, a.h
    return {"file_name": name, "bytes": getattr(doc, "size", None),
            "mime": getattr(doc, "mime_type", None),
            "width": w, "height": h, "duration_s": dur}


def capture(msg, meta: dict) -> dict | None:
    """Persist one message. Returns the new row, or None if already stored.

    Deliberately tolerant: a message that cannot be parsed is still recorded with
    whatever is available, because losing it is worse than storing it imperfectly.
    """
    urls = ingest.extract_urls(msg)
    text = msg.message or ""
    fm = file_meta(msg)
    return adb.record_event(
        channel_id=meta["channel_id"], message_id=msg.id,
        channel_title=meta["title"], tier=meta["tier"],
        posted_at=msg.date.astimezone(timezone.utc) if msg.date else None,
        text=text, urls=urls,
        has_media=bool(msg.media), media_kind=ingest.media_kind(msg),
        content_hash=ingest.content_hash(text, urls),
        raw={"file": fm} if fm else None,
        vertical=meta.get("vertical"),
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


async def run(vertical=None, account: str | None = None,
              duration_s: int | None = None) -> dict:
    """Watch one account. `vertical` may be one name, several, or "*".

    "*" means every enabled vertical that draws from this account, which is the form the
    supervisor uses: adding a niche to a shared account then needs no code change and no
    second process, because a second process is the one thing that cannot be allowed —
    only one client may hold a session's lease.
    """
    adb.migrate()
    if vertical == "*":
        if not account:
            raise ValueError("vertical='*' needs an account to resolve against")
        names = verticals_on(account) or [active.name()]
    elif isinstance(vertical, str) or vertical is None:
        names = [vertical or active.name()]
    else:
        names = list(vertical)

    if not account:
        for n in names:
            try:
                account = (verticals.load_any(n).get("source") or {}).get("account")
            except Exception:
                account = None
            if account:
                break

    chans = watched_channels(names, account)
    stats = {"replayed": 0, "live": 0, "duplicates": 0, "errors": 0,
             "verticals": names}

    if not adb.acquire_lease(account, WORKER_ID, LEASE_SECONDS):
        log(f"another watcher holds the lease for '{account}' — refusing to start. "
            f"Two clients on one session risk the auth key being revoked.")
        return {**stats, "refused": True}
    by_v = {}
    for m in chans.values():
        by_v[m["vertical"]] = by_v.get(m["vertical"], 0) + 1
    log(f"lease acquired: worker {WORKER_ID}, account '{account}', "
        f"{len(chans)} channels in {sorted(WATCHED_TIERS)} across "
        f"{len(by_v)} vertical(s): {by_v}")

    stop = asyncio.Event()

    # Everything below runs inside a try/finally so the lease is released on EVERY exit
    # path, not just the tidy one.
    #
    # It used to be released by a bare statement after the `async with`, which a
    # cancellation or an exception skipped entirely. That matters twice over now:
    # `docker stop -t 60 tg-agent` sends SIGTERM and this function installs no handler
    # when `duration_s` is set (which the supervisor always does), so the old path left
    # the lease held until its 300-second TTL expired — exactly the window the next
    # runner needs it in. And the supervisor now runs one of these per account, so a
    # cancelled sibling must not strand a session either.
    try:
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

    finally:
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
