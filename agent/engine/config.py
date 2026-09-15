"""Configuration, resolved the same way everywhere.

Locally the source is .env.telegram-app in the project root. On a GitHub runner
or the EC2 there is no such file and the values arrive as environment variables,
so the file is optional and real env vars always win.

The Supabase password is deliberately NOT duplicated here -- it is read from
../keys/CREDENTIALS.env, which already holds it. One copy, one place to rotate.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env.telegram-app"
CREDENTIALS = ROOT.parent / "keys" / "CREDENTIALS.env"
EXPORTS = ROOT / "exports"
STATE_DIR = ROOT / ".state"


def parse_env_file(path: Path, prefix: str = "") -> dict:
    """Plain KEY=VALUE, '#' comments, trailing ' # note' stripped."""
    if not path.is_file():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if not prefix or k.startswith(prefix):
            out[k] = v.split(" #")[0].split("\t#")[0].strip()
    return out


def load(require: tuple = ()) -> dict:
    env = parse_env_file(ENV_FILE)
    for k, v in os.environ.items():
        if k.startswith(("TG_", "SB_", "YT_", "IG_")) and v.strip():
            env[k] = v.strip()
    for key in require:
        if not env.get(key):
            where = ENV_FILE.name if ENV_FILE.is_file() else "the environment"
            raise SystemExit(f"{key} is not set (checked {where} and env vars)")
    return env


def db_config() -> dict:
    """Supabase connection: SB_* if given, else SUPABASE_DB_* from CREDENTIALS.env."""
    cfg = {}
    creds = parse_env_file(CREDENTIALS, "SUPABASE_DB_")
    if creds:
        cfg = {
            "host": creds.get("SUPABASE_DB_HOST"),
            "port": int(creds.get("SUPABASE_DB_PORT") or 5432),
            "dbname": creds.get("SUPABASE_DB_NAME") or "postgres",
            "user": creds.get("SUPABASE_DB_USER"),
            "password": creds.get("SUPABASE_DB_PASSWORD"),
        }

    env = load()
    for src, dst in (("SB_HOST", "host"), ("SB_PORT", "port"), ("SB_DB", "dbname"),
                     ("SB_USER", "user"), ("SB_PASSWORD", "password")):
        if env.get(src):
            cfg[dst] = int(env[src]) if dst == "port" else env[src]

    missing = [k for k in ("host", "user", "password") if not cfg.get(k)]
    if missing:
        raise SystemExit(
            f"Supabase config incomplete (missing {', '.join(missing)}).\n"
            f"Set SB_HOST/SB_USER/SB_PASSWORD, or keep SUPABASE_DB_* in {CREDENTIALS}."
        )
    cfg.setdefault("dbname", "postgres")
    cfg.setdefault("port", 5432)
    cfg["sslmode"] = "require"
    cfg["connect_timeout"] = 20
    return cfg


def get(key: str, default=None):
    """One value, resolved the way every other secret in this project is.

    Order: real environment variable, then `.env.<project>`, then `keys/CREDENTIALS.env`.
    The SMTP credentials live in the shared CREDENTIALS file because the EC2 backup
    scripts already use them and there should be exactly one copy to rotate; the
    Telegram and model keys live in the project env file. Callers should not have to
    know which is which.
    """
    v = os.environ.get(key)
    if v and v.strip():
        return v.strip()
    v = parse_env_file(ENV_FILE).get(key)
    if v and v.strip():
        return v.strip()
    v = parse_env_file(CREDENTIALS).get(key)
    if v and v.strip():
        return v.strip()
    return default
