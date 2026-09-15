"""Database access. Everything durable lives in Supabase, nothing on local disk,
so this machine / the EC2 / an ephemeral runner are interchangeable.
"""

from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras

from engine import config

SCHEMA_SQL = Path(__file__).resolve().parent / "schema.sql"


_conn = None


def _live():
    """One connection, reused. Ingest writes a row per message, and opening a fresh
    SSL connection to the Supabase pooler each time costs more than the query does.
    Reconnects by itself if the pooler drops it."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(**config.db_config())
        _conn.autocommit = True
    return _conn


def close() -> None:
    global _conn
    if _conn is not None and not _conn.closed:
        _conn.close()
    _conn = None


@contextmanager
def connect():
    """Explicit transaction block: commits on success, rolls back on error.

    psycopg2's own `with conn` commits but never closes -- and here the connection
    is shared and deliberately outlives the block, so it must not be closed at all.
    """
    conn = _live()
    prior = conn.autocommit
    conn.autocommit = False
    try:
        with conn:
            yield conn
    finally:
        conn.autocommit = prior


@contextmanager
def cursor(dict_rows: bool = False):
    """A cursor on the shared connection, autocommitting each statement."""
    factory = psycopg2.extras.RealDictCursor if dict_rows else None
    try:
        conn = _live()
        cur = conn.cursor(cursor_factory=factory)
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        close()                       # stale handle -> drop it and dial again
        conn = _live()
        cur = conn.cursor(cursor_factory=factory)
    try:
        yield cur
    finally:
        cur.close()


def migrate() -> None:
    """Apply schema.sql. Idempotent -- every statement is IF NOT EXISTS."""
    with cursor() as cur:
        cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))


def fetch_all(sql: str, params=()) -> list:
    with cursor(dict_rows=True) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def fetch_one(sql: str, params=()) -> dict | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=()) -> int:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def insert_many(sql: str, rows: list, fetch: bool = False) -> list:
    """Multi-row INSERT in one round trip. `sql` must contain a single %s where the
    VALUES tuples go. Round-trip latency to Supabase is ~200ms, so inserting a few
    thousand messages one at a time would take minutes; this makes it one call per
    batch. With fetch=True, returns whatever the RETURNING clause yields."""
    if not rows:
        return []
    with cursor() as cur:
        return psycopg2.extras.execute_values(cur, sql, rows, page_size=500, fetch=fetch) or []


def upsert(table: str, key_col: str, key_val, **fields) -> None:
    """Update the row if present, else insert it. Only the given fields are touched.

    UPDATE-then-INSERT rather than ON CONFLICT: Postgres validates NOT NULL on the
    proposed tuple before it arbitrates the conflict, so a partial update would trip
    a NOT NULL column even though the insert would never land.
    """
    fields = {k: v for k, v in fields.items() if v is not None}
    with cursor() as cur:
        if fields:
            setters = ", ".join(f"{k} = %s" for k in fields)
            cur.execute(
                f"UPDATE {table} SET {setters}, updated_at = now() WHERE {key_col} = %s",
                list(fields.values()) + [key_val],
            )
            if cur.rowcount:
                return
        else:
            cur.execute(f"SELECT 1 FROM {table} WHERE {key_col} = %s", (key_val,))
            if cur.fetchone():
                return
        cols = [key_col] + list(fields)
        cur.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
            [key_val] + list(fields.values()),
        )


def status() -> list:
    """Row counts per table in the content schema -- a quick health check."""
    tables = fetch_all(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'content' ORDER BY tablename"
    )
    out = []
    for t in tables:
        name = t["tablename"]
        n = fetch_one(f"SELECT count(*) AS n FROM content.{name}")["n"]
        out.append((name, n))
    return out
