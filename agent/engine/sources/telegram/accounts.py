"""Telegram account records in content.source_accounts."""

from engine import db

TABLE = "content.source_accounts"


def get(label: str) -> dict | None:
    return db.fetch_one(f"SELECT * FROM {TABLE} WHERE label = %s", (label,))


def all_(active_only: bool = False) -> list:
    where = "WHERE is_active" if active_only else ""
    return db.fetch_all(f"SELECT * FROM {TABLE} {where} ORDER BY label")


def save(label: str, **fields) -> None:
    db.upsert(TABLE, "label", label, **fields)


def touch(label: str) -> None:
    db.execute(f"UPDATE {TABLE} SET last_used_at = now() WHERE label = %s", (label,))


def mark_login(label: str) -> None:
    db.execute(f"UPDATE {TABLE} SET last_login_at = now() WHERE label = %s", (label,))


def remove(label: str) -> int:
    return db.execute(f"DELETE FROM {TABLE} WHERE label = %s", (label,))


def resolve(label: str | None) -> dict:
    """The named account, or the only one if there is exactly one active."""
    if label:
        row = get(label)
        if not row:
            known = ", ".join(a["label"] for a in all_()) or "(none)"
            raise SystemExit(f"no account '{label}'. Known: {known}")
        return row

    rows = all_(active_only=True)
    if not rows:
        raise SystemExit("no accounts yet.  run.py tg accounts add <label> --phone +1...")
    if len(rows) > 1:
        raise SystemExit(
            "several accounts exist -- pass --account <label>: "
            + ", ".join(a["label"] for a in rows)
        )
    return rows[0]
