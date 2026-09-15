"""Sign-in. Two steps, because only the person holding the phone can read the code.

The session lands in content.source_accounts, so it is a one-time cost per account
for the whole estate -- every machine pointed at the database is then signed in.
"""

import json
import sys
import time

from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from engine import config
from engine.sources.telegram import accounts, client as tgclient

STATE = config.STATE_DIR / "login.json"


def _read() -> dict:
    return json.loads(STATE.read_text()) if STATE.is_file() else {}


def _write(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def pending() -> dict:
    return _read()


async def _persist(client, label: str) -> None:
    me = await client.get_me()
    accounts.save(
        label,
        session_string=StringSession.save(client.session),
        user_id=me.id,
        username=me.username,
        first_name=me.first_name,
        phone=f"+{me.phone}" if me.phone else None,
    )
    accounts.mark_login(label)
    handle = f"@{me.username}" if me.username else "(no username)"
    print(f"\nSigned in as {me.first_name or ''} {handle}  id={me.id}")
    print(f"Session saved to {accounts.TABLE} (label '{label}')")


async def _finish(client, label, env, phone, code, code_hash) -> int:
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
    except PhoneCodeInvalidError:
        print("Wrong code. Re-run with the correct --code (that code is still valid).")
        return 1
    except PhoneCodeExpiredError:
        st = _read(); st.pop(label, None); _write(st)
        print("Code expired. Request a fresh one.")
        return 1
    except SessionPasswordNeededError:
        pw = env.get("TG_PASSWORD")
        if not pw:
            try:
                pw = input("Two-step verification password: ")
            except EOFError:
                print("2FA is on for this account but TG_PASSWORD is empty.")
                return 1
        print("2FA enabled -- submitting cloud password …")
        await client.sign_in(password=pw)

    await _persist(client, label)
    st = _read(); st.pop(label, None); _write(st)
    return 0


async def login(label: str | None, code: str | None = None, phone_override: str | None = None) -> int:
    env = config.load()
    row = accounts.resolve(label)
    label = row["label"]
    phone = (phone_override or row.get("phone") or "").strip()
    if not phone:
        print(f"No phone for '{label}'.  run.py tg accounts add {label} --phone +1...")
        return 2

    client = tgclient.build(row)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"'{label}' is already signed in as {me.first_name or ''} (id={me.id}).")
            return 0

        # ---- step 2 ----
        if code:
            st = _read().get(label)
            if not st:
                print(f"No pending code request for '{label}'. Request one first.")
                return 1
            if st.get("phone") != phone:
                print("Pending request is for a different number. Request a new code.")
                return 1
            if not st.get("session"):
                print("That pending request predates the session fix. Request a new code.")
                return 1
            # A phone_code_hash is only valid for the auth key that requested it, and
            # step 1 ran in a different process -- so rebuild THAT session rather than
            # a fresh one, or Telegram rejects the code as expired seconds later.
            await client.disconnect()
            client = tgclient.build({**row, "session_string": st["session"]})
            await client.connect()
            print(f"Completing sign-in for '{label}' ({int(time.time() - st['at'])}s after the request) …")
            return await _finish(client, label, env, phone, code.strip(), st["hash"])

        # ---- step 1 ----
        print(f"Requesting a login code for '{label}' ({phone}) …")
        try:
            sent = await client.send_code_request(phone)
        except PhoneNumberInvalidError:
            print("Telegram says that number is invalid. Check the country code.")
            return 1
        except ApiIdInvalidError:
            print("api_id/api_hash rejected.")
            return 1
        except FloodWaitError as e:
            print(f"Rate-limited. Wait {e.seconds}s ({e.seconds // 60} min).")
            return 1

        st = _read()
        st[label] = {
            "phone": phone,
            "hash": sent.phone_code_hash,
            "at": time.time(),
            "session": StringSession.save(client.session),  # the code is bound to THIS key
        }
        _write(st)
        print(f"Code sent via: {type(sent.type).__name__.replace('SentCodeType', '')}")

        # isatty() lies in some shells (Git Bash reports a tty even with stdin
        # redirected), so treat EOF as non-interactive instead of crashing after the
        # code has already gone out.
        try:
            if sys.stdin.isatty():
                return await _finish(client, label, env, phone,
                                     input("Login code: ").strip(), sent.phone_code_hash)
        except EOFError:
            pass
        print(f"\nThen run:  run.py tg login --account {label} --code <the code>")
        return 0
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
