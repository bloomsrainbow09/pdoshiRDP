"""Telethon client construction from a stored account row."""

from contextlib import asynccontextmanager

from telethon import TelegramClient
from telethon.sessions import StringSession

from engine.sources.telegram import accounts


def build(row: dict) -> TelegramClient:
    """A client for one account row.

    The session comes from the row, so this machine, the EC2 and any runner build
    the same authenticated client from the same database -- nothing to copy when
    the work moves. device_model carries the label so Telegram -> Settings ->
    Devices shows which authorization is which.
    """
    return TelegramClient(
        StringSession(row.get("session_string") or ""),
        int(row["api_id"]),
        row["api_hash"],
        device_model=f"automation:{row['label']}",
        system_version="Linux",
        app_version="1.0",
    )


@asynccontextmanager
async def connected(label: str | None, require_auth: bool = True):
    """Resolve an account, connect, and always disconnect. Yields (client, row)."""
    row = accounts.resolve(label)
    client = build(row)
    await client.connect()
    try:
        if require_auth:
            if not row.get("session_string"):
                raise SystemExit(
                    f"'{row['label']}' is not signed in.  run.py tg login --account {row['label']}"
                )
            if not await client.is_user_authorized():
                raise SystemExit(
                    f"'{row['label']}' session was rejected -- it was probably ended in "
                    f"Telegram -> Settings -> Devices. Log in again."
                )
            accounts.touch(row["label"])
        yield client, row
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
