#!/usr/bin/env python
"""Content engine CLI.

    run.py db init | status

    run.py tg accounts list [--secrets] | add <label> --phone +1... | remove <label>
    run.py tg accounts enable <label> | disable <label>
    run.py tg login   --account <label> [--code 12345]
    run.py tg who     [--account <label> | --all] [--dialogs N]
    run.py tg folders [--account <label>] [--folder Trading] [--csv]
    run.py tg ingest  [--account <label>] @chan [...] [--folder F] [--limit N]
                      [--since YYYY-MM-DD] [--all] [--media]
    run.py tg ingest  --account india --from-folder Trading      # every chat in a folder
    run.py tg ingest  --vertical trading                         # every channel a niche defines

    run.py vertical list | show <name> | sync <name>
    run.py vertical download <name> [--media] [--limit N]   # incremental, auto-discovers
    run.py vertical status <name>
    run.py vertical reap <name> [--days 60] [--yes] [--leave]   # retire dead channels
    run.py vertical daily <name>                                # THE SCHEDULED RUN

    run.py media ocr [--vertical trading] [--limit N] [--provider nvidia]
    run.py media prune [--keep-days 1] [--dry-run]   # yesterday's images go
    run.py media status | models

Later, on the same spine:  run.py yt publish / run.py ig publish
"""

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime, timezone

from engine import archive, config, db, tierdocs, tiers, verticals
from engine.content import ocr as ocrmod
from engine.media import lifecycle
from engine.sources.telegram import accounts, auth, browse, ingest


# ------------------------------------------------------------- verticals -----

def cmd_vert_list(a):
    names = verticals.available()
    if not names:
        print("no verticals. Create verticals/<name>/vertical.json")
        return 0
    live = {r["name"]: r for r in db.fetch_all("SELECT * FROM content.verticals")}
    for n in names:
        cfg = verticals.load(n)
        synced = live[n]["synced_at"].strftime("%Y-%m-%d %H:%M") if n in live else "never"
        # `.get('account', '?')` returns None rather than '?' when the key exists and is
        # null, which a vertical with no Telegram account of its own legitimately has —
        # verticals/demo/ and verticals/_template/ both do. `or '-'` covers both cases.
        account = (cfg.get("source") or {}).get("account") or "-"
        print(f"  {n:<12} {len(verticals.channels(cfg)):>3} channels  "
              f"source={account:<8} synced={synced}")
    return 0


def cmd_vert_show(a):
    cfg = verticals.load(a.name)
    print(json.dumps(cfg, indent=2, ensure_ascii=False))
    return 0


def cmd_vert_download(a):
    async def go():
        db.migrate()
        r = (await archive.download_until_stable(a.name, a.limit, a.media)
             if a.until_stable else await archive.download(a.name, a.limit, a.media, a.only))
        print(f"\ndone — {r['channels']} channel(s), +{r['new']} new message(s), "
              f"{r['total']} on disk"
              + (f", {r['added_channels']} channel(s) newly discovered" if r["added_channels"] else ""))
        return 0
    return asyncio.run(go())


def cmd_vert_status(a):
    man = archive.read_manifest(a.name)
    chans = man.get("channels", {})
    if not chans:
        print(f"nothing downloaded yet.  run.py vertical download {a.name}")
        return 0
    print(f"{'channel':<42}{'messages':>10}{'last id':>12}  {'last run':<17}in folder")
    print("-" * 92)
    for cid, c in sorted(chans.items(), key=lambda kv: -kv[1].get("messages", 0)):
        run = (c.get("last_run") or "")[:16].replace("T", " ")
        print(f"{(c.get('title') or cid)[:40]:<42}{c.get('messages', 0):>10}"
              f"{c.get('last_message_id', 0):>12}  {run:<17}{'yes' if c.get('in_folder', True) else 'no'}")
    print(f"\n{len(chans)} channel(s), {sum(c.get('messages', 0) for c in chans.values())} messages on disk")
    return 0


def cmd_vert_organize(a):
    tiers.organize(a.name, dry_run=not a.yes)
    if a.yes:
        for f in tierdocs.write(a.name):
            print(f"  wrote {f.relative_to(archive.vdir(a.name))}")
    return 0


def cmd_vert_docs(a):
    for f in tierdocs.write(a.name):
        print(f"  wrote {f.relative_to(archive.vdir(a.name))}")
    return 0


def cmd_vert_tier(a):
    tiers.set_place(a.name, int(a.channel_id), a.tier, a.category or "", a.note or "")
    print(f"{a.channel_id} -> {a.tier}/{a.category or ''}".rstrip("/"))
    print("run:  run.py vertical organize " + a.name + " --yes")
    return 0


def cmd_vert_daily(a):
    """The whole daily cycle in one command — what the scheduler runs."""
    async def go():
        db.migrate()
        bar = "=" * 70
        print(f"{bar}\n1/2  DOWNLOAD  (auto-discovers new channels, incremental)\n{bar}")
        r = await archive.download_until_stable(a.name, None, a.media)
        print(f"\n  {r['channels']} channel(s), +{r['new']} new, {r['total']} on disk"
              + (f", {r['added_channels']} newly discovered" if r["added_channels"] else ""))
        print(f"\n{bar}\n2/2  REAP  (retire channels silent {a.days}+ days)\n{bar}")
        # Approved as fully automatic: local delete AND leave on Telegram. Channels
        # with no public username are still skipped -- losing a paid/private channel
        # is not recoverable, so that one stays an explicit human decision.
        await archive.reap(a.name, a.days, apply=True, leave=True, leave_private=False)
        return 0
    return asyncio.run(go())


def cmd_vert_reap(a):
    async def go():
        db.migrate()
        await archive.reap(a.name, a.days, a.yes, a.leave, a.leave_private)
        return 0
    return asyncio.run(go())


def cmd_vert_sync(a):
    db.migrate()
    r = verticals.sync(a.name)
    print(f"synced '{r['vertical']}' — {r['channels']} channel(s) defined, "
          f"{r['tagged_existing']} already-ingested channel(s) re-tagged")
    return 0


# ---------------------------------------------------------------- media ------

def cmd_media_ocr(a):
    db.migrate()
    prov, mdl = ocrmod.defaults()
    lifecycle.run_ocr(a.limit, a.vertical, a.provider or prov, a.model or mdl)
    return 0


def cmd_media_fetch(a):
    db.migrate()
    asyncio.run(lifecycle.fetch_recent(a.vertical, a.days, a.limit, a.account))
    return 0


def cmd_media_roll(a):
    lifecycle.roll_media(a.vertical, a.keep_days, a.dry_run)
    return 0


def cmd_media_prune(a):
    lifecycle.prune(a.keep_days, a.force, a.dry_run)
    return 0


def cmd_media_status(a):
    lifecycle.status()
    return 0


def cmd_media_models(a):
    for m in ocrmod.list_models(a.provider, not a.all):
        print(" ", m)
    return 0


# ------------------------------------------------------------------- db ------

def cmd_db_init(a):
    db.migrate()
    print("schema applied. tables in content:")
    for name, n in db.status():
        print(f"  {name:<18} {n:>8} rows")
    return 0


def cmd_db_status(a):
    for name, n in db.status():
        print(f"  {name:<18} {n:>8} rows")
    return 0


# ------------------------------------------------------------- tg accounts ---

def cmd_acc_list(a):
    rows = accounts.all_()
    if not rows:
        print("no accounts.  run.py tg accounts add <label> --phone +1...")
        return 0
    print(f"{'label':<12}{'phone':<17}{'signed in':<11}{'username':<18}{'last used':<18}note")
    print("-" * 96)
    for r in rows:
        signed = "yes" if r.get("session_string") else "no"
        if not r["is_active"]:
            signed += " (off)"
        used = r["last_used_at"].strftime("%Y-%m-%d %H:%M") if r.get("last_used_at") else "—"
        uname = f"@{r['username']}" if r.get("username") else (r.get("first_name") or "—")
        print(f"{r['label']:<12}{r.get('phone') or '—':<17}{signed:<11}{uname:<18}{used:<18}{r.get('note') or ''}")
        if a.secrets and r.get("session_string"):
            print(f"{'':<12}session: {r['session_string']}")
    if not a.secrets and any(r.get("session_string") for r in rows):
        print("\n(--secrets reveals session strings — each is full control of that account)")
    return 0


def cmd_acc_add(a):
    env = config.load(require=("TG_API_ID", "TG_API_HASH"))
    existed = accounts.get(a.label)
    accounts.save(a.label, phone=a.phone, platform="telegram",
                  api_id=int(a.api_id or env["TG_API_ID"]),
                  api_hash=a.api_hash or env["TG_API_HASH"], note=a.note)
    print(f"{'updated' if existed else 'added'} '{a.label}'  phone={a.phone or '—'}")
    if not (existed and existed.get("session_string")):
        print(f"next:  run.py tg login --account {a.label}")
    return 0


def cmd_acc_remove(a):
    row = accounts.get(a.label)
    if not row:
        print(f"no account '{a.label}'")
        return 1
    if row.get("session_string") and not a.force:
        print(f"'{a.label}' is signed in. Deleting the row does NOT end the Telegram")
        print("session — do that in Telegram → Settings → Devices. Use --force to proceed.")
        return 1
    print(f"removed '{a.label}' ({accounts.remove(a.label)} row)")
    return 0


def cmd_acc_active(a, on):
    if not accounts.get(a.label):
        print(f"no account '{a.label}'")
        return 1
    accounts.save(a.label, is_active=on)
    print(f"'{a.label}' {'enabled' if on else 'disabled'}")
    return 0


# ------------------------------------------------------------- tg actions ----

def cmd_login(a):
    return asyncio.run(auth.login(a.account, a.code, a.phone))


def cmd_who(a):
    async def go():
        results = await browse.whoami_all(a.dialogs) if a.all else [await browse.whoami(a.account, a.dialogs)]
        bad = 0
        for r in results:
            print(f"\n=== {r['label']} ===")
            if r.get("error"):
                print(f"  {r['error']}")
                bad += 1
                continue
            me = r["me"]
            handle = f"@{me.username}" if me.username else "(no username)"
            print(f"  {me.first_name or ''} {handle} id={me.id} +{me.phone}")
            for c in r["chats"]:
                unread = f"  ({c['unread']} unread)" if c["unread"] else ""
                print(f"    [{c['kind']:7}] {c['name'][:50]:52} id={c['id']}{unread}")
        print(f"\n{len(results) - bad}/{len(results)} account(s) reachable.")
        return 1 if bad else 0
    return asyncio.run(go())


def cmd_folders(a):
    async def go():
        found = await browse.folders(a.account, a.folder)
        if not found:
            print("no matching folders." if a.folder else "no chat folders on this account.")
            return 1
        if not a.folder:
            print("chat folders:\n")
            for title, chats in found:
                print(f"  {title:<28} {len(chats)} chat(s)")
            print('\nList one with:  run.py tg folders --folder "<name>"')
            return 0
        for title, chats in found:
            if a.csv:
                w = csv.DictWriter(sys.stdout, fieldnames=["folder", "kind", "name", "username", "id", "unread"],
                                   lineterminator="\n")
                w.writeheader()
                for c in chats:
                    w.writerow({"folder": title, **c,
                                "username": f"@{c['username']}" if c["username"] else ""})
                continue
            print(f"\n=== {title} — {len(chats)} chat(s) ===\n")
            print(f"{'#':<4}{'type':<9}{'name':<44}{'username':<24}{'id':<16}unread")
            print("-" * 106)
            for i, c in enumerate(chats, 1):
                uname = f"@{c['username']}" if c["username"] else ""
                print(f"{i:<4}{c['kind']:<9}{c['name'][:42]:<44}{uname:<24}{str(c['id']):<16}{c['unread'] or ''}")
        return 0
    return asyncio.run(go())


def cmd_ingest(a):
    async def go():
        db.migrate()
        targets = list(a.channels)
        folder = a.folder
        if a.vertical:
            cfg = verticals.load(a.vertical)
            chans = verticals.channels(cfg)
            folder = cfg.get("display_name") or a.vertical
            a.account = a.account or cfg.get("source", {}).get("account")
            targets += [c["id"] for c in chans]
            print(f"vertical '{a.vertical}': {len(chans)} channel(s), account '{a.account}'")
        if a.from_folder:
            found = await browse.folders(a.account, a.from_folder)
            if not found:
                print(f"no folder matching '{a.from_folder}'")
                return 1
            folder = found[0][0]
            targets += [c["id"] for c in found[0][1]]
            print(f"folder '{folder}': {len(found[0][1])} chat(s)")
        if not targets:
            print("nothing to ingest — pass channels or --from-folder")
            return 1
        since = (datetime.strptime(a.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                 if a.since else None)
        media_dir = (config.EXPORTS / "media") if a.media else None
        res = await ingest.run(a.account, targets, a.limit, since, a.all, folder, media_dir)
        print(f"\ndone — {sum(r['new'] for r in res)} new item(s) across {len(res)} channel(s)")
        return 0
    return asyncio.run(go())


# ----------------------------------------------------------------- parser ----

def build_parser():
    ap = argparse.ArgumentParser(prog="run.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    top = ap.add_subparsers(dest="group", required=True)

    d = top.add_parser("db", help="schema and health").add_subparsers(dest="cmd", required=True)
    d.add_parser("init", help="create/update the content schema").set_defaults(fn=cmd_db_init)
    d.add_parser("status", help="row counts").set_defaults(fn=cmd_db_status)

    tg = top.add_parser("tg", help="Telegram source").add_subparsers(dest="cmd", required=True)

    acc = tg.add_parser("accounts", help="manage accounts").add_subparsers(dest="sub", required=True)
    p = acc.add_parser("list"); p.add_argument("--secrets", action="store_true"); p.set_defaults(fn=cmd_acc_list)
    p = acc.add_parser("add"); p.add_argument("label"); p.add_argument("--phone")
    p.add_argument("--api-id"); p.add_argument("--api-hash"); p.add_argument("--note")
    p.set_defaults(fn=cmd_acc_add)
    p = acc.add_parser("remove"); p.add_argument("label"); p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_acc_remove)
    p = acc.add_parser("enable"); p.add_argument("label"); p.set_defaults(fn=lambda a: cmd_acc_active(a, True))
    p = acc.add_parser("disable"); p.add_argument("label"); p.set_defaults(fn=lambda a: cmd_acc_active(a, False))

    p = tg.add_parser("login"); p.add_argument("--account"); p.add_argument("--code")
    p.add_argument("--phone"); p.set_defaults(fn=cmd_login)

    p = tg.add_parser("who"); p.add_argument("--account"); p.add_argument("--all", action="store_true")
    p.add_argument("--dialogs", type=int, default=15); p.set_defaults(fn=cmd_who)

    p = tg.add_parser("folders"); p.add_argument("--account"); p.add_argument("--folder")
    p.add_argument("--csv", action="store_true"); p.set_defaults(fn=cmd_folders)

    p = tg.add_parser("ingest"); p.add_argument("channels", nargs="*")
    p.add_argument("--account"); p.add_argument("--from-folder")
    p.add_argument("--vertical", help="ingest every channel in verticals/<name>/")
    p.add_argument("--folder", help="tag the channels with this folder name")
    p.add_argument("--limit", type=int); p.add_argument("--since")
    p.add_argument("--all", action="store_true", help="ignore the stored resume point")
    p.add_argument("--media", action="store_true"); p.set_defaults(fn=cmd_ingest)

    m = top.add_parser("media", help="images: OCR then discard").add_subparsers(dest="cmd", required=True)
    p = m.add_parser("ocr", help="read pending images into text")
    p.add_argument("--limit", type=int, default=100); p.add_argument("--vertical")
    p.add_argument("--provider", choices=list(ocrmod.PROVIDERS),
                   help="overrides OCR_PROVIDER in .env.telegram-app")
    p.add_argument("--model", help="overrides OCR_MODEL"); p.set_defaults(fn=cmd_media_ocr)
    p = m.add_parser("fetch", help="download image bytes for recent items")
    p.add_argument("vertical"); p.add_argument("--days", type=int, default=1)
    p.add_argument("--limit", type=int, default=200); p.add_argument("--account")
    p.set_defaults(fn=cmd_media_fetch)
    p = m.add_parser("roll", help="keep only the latest day of media per channel")
    p.add_argument("vertical"); p.add_argument("--keep-days", type=int, default=1)
    p.add_argument("--dry-run", action="store_true"); p.set_defaults(fn=cmd_media_roll)
    p = m.add_parser("prune", help="delete old image files, keep the OCR text")
    p.add_argument("--keep-days", type=int, default=1)
    p.add_argument("--force", action="store_true", help="also drop images never OCR'd")
    p.add_argument("--dry-run", action="store_true"); p.set_defaults(fn=cmd_media_prune)
    m.add_parser("status").set_defaults(fn=cmd_media_status)
    p = m.add_parser("models", help="what the API key currently reaches")
    p.add_argument("--provider", default="nvidia", choices=list(ocrmod.PROVIDERS))  # listing is provider-specific
    p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_media_models)

    v = top.add_parser("vertical", help="niches (verticals/<name>/)").add_subparsers(dest="cmd", required=True)
    v.add_parser("list").set_defaults(fn=cmd_vert_list)
    p = v.add_parser("show"); p.add_argument("name"); p.set_defaults(fn=cmd_vert_show)
    p = v.add_parser("sync"); p.add_argument("name"); p.set_defaults(fn=cmd_vert_sync)
    p = v.add_parser("download", help="download every channel in the vertical's folder")
    p.add_argument("name"); p.add_argument("--limit", type=int, help="cap per channel (testing)")
    p.add_argument("--media", action="store_true"); p.add_argument("--only", help="one channel id/@username")
    p.add_argument("--until-stable", action="store_true",
                   help="repeat until a pass finds no new channels (catches ones added mid-run)")
    p.set_defaults(fn=cmd_vert_download)
    p = v.add_parser("status", help="what is on disk"); p.add_argument("name")
    p.set_defaults(fn=cmd_vert_status)
    p = v.add_parser("organize", help="move files into their tier/category folders")
    p.add_argument("name"); p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_vert_organize)
    p = v.add_parser("docs", help="regenerate the TIER*.md explanations")
    p.add_argument("name"); p.set_defaults(fn=cmd_vert_docs)
    p = v.add_parser("tier", help="place one channel in a tier/category")
    p.add_argument("name"); p.add_argument("channel_id")
    p.add_argument("tier", choices=list(tiers.TIER_LABELS))
    p.add_argument("--category"); p.add_argument("--note")
    p.set_defaults(fn=cmd_vert_tier)
    p = v.add_parser("daily", help="the scheduled run: download + reap")
    p.add_argument("name"); p.add_argument("--days", type=int, default=archive.DEAD_AFTER_DAYS)
    p.add_argument("--media", action="store_true"); p.set_defaults(fn=cmd_vert_daily)
    p = v.add_parser("reap", help="retire channels silent for 60+ days")
    p.add_argument("name")
    p.add_argument("--days", type=int, default=archive.DEAD_AFTER_DAYS)
    p.add_argument("--yes", action="store_true", help="actually delete (default is a dry run)")
    p.add_argument("--leave", action="store_true", help="also leave the channel on Telegram")
    p.add_argument("--leave-private", action="store_true",
                   help="leave even channels with no public username (paid/private)")
    p.set_defaults(fn=cmd_vert_reap)

    return ap


if __name__ == "__main__":
    args = build_parser().parse_args()
    raise SystemExit(args.fn(args))
