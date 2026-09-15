"""Read-only discovery: who am I, what chats exist, what chat folders exist.

Chat folders are dialog filters -- a different API from the dialog list -- so
folders() resolves members against one dialog sweep rather than an API call per
chat, which would rate-limit on a large folder.
"""

import asyncio

from telethon import utils
from telethon.tl.functions.messages import GetDialogFiltersRequest

from engine.sources.telegram import accounts, client as tgclient


def _folder_title(f) -> str:
    """Plain str on old layers, TextWithEntities on new ones."""
    t = getattr(f, "title", None)
    return getattr(t, "text", t) or ""


def kind_of(entity) -> str:
    if entity is None:
        return "?"
    if getattr(entity, "broadcast", False):
        return "channel"
    if getattr(entity, "megagroup", False) or entity.__class__.__name__ in ("Chat", "ChatForbidden"):
        return "group"
    return "user"


async def whoami(label: str | None, dialog_limit: int = 15) -> dict:
    async with tgclient.connected(label) as (client, row):
        me = await client.get_me()
        chats = []
        async for d in client.iter_dialogs(limit=dialog_limit):
            chats.append({
                "kind": kind_of(d.entity), "name": str(d.name),
                "id": utils.get_peer_id(d.entity), "unread": d.unread_count,
                "username": getattr(d.entity, "username", None),
            })
        return {"label": row["label"], "me": me, "chats": chats}


async def whoami_all(dialog_limit: int = 15) -> list:
    """Different accounts are independent authorizations, so this is safe in parallel.
    (Sharing ONE session across clients is the dangerous case, not this.)"""
    rows = accounts.all_(active_only=True)
    if not rows:
        raise SystemExit("no accounts yet.")

    async def one(r):
        try:
            return await whoami(r["label"], dialog_limit)
        except SystemExit as e:
            return {"label": r["label"], "error": str(e), "chats": []}
        except Exception as e:
            return {"label": r["label"], "error": f"{type(e).__name__}: {e}", "chats": []}

    return await asyncio.gather(*(one(r) for r in rows))


async def folders(label: str | None, name: str | None = None) -> list:
    """[(folder_title, [chat dicts])]. With `name`, only matching folders."""
    async with tgclient.connected(label) as (client, row):
        by_id = {}
        async for d in client.iter_dialogs():
            by_id[utils.get_peer_id(d.entity)] = d

        res = await client(GetDialogFiltersRequest())
        raw = getattr(res, "filters", res)  # newer layers wrap the list

        out = []
        for f in raw:
            if f.__class__.__name__ == "DialogFilterDefault":
                continue
            title = _folder_title(f)
            if name and name.lower() not in title.lower():
                continue
            peers = list(getattr(f, "pinned_peers", [])) + list(getattr(f, "include_peers", []))
            chats = []
            for p in peers:
                pid = utils.get_peer_id(p)
                d = by_id.get(pid)
                ent = d.entity if d else None
                chats.append({
                    "id": pid,
                    "kind": kind_of(ent),
                    "name": str(d.name) if d else "(not in dialog list)",
                    "username": getattr(ent, "username", None),
                    "unread": d.unread_count if d else None,
                })
            out.append((title, chats))
        return out
