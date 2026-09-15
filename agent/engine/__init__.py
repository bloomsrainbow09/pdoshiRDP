"""Content engine: Telegram (and later other sources) -> renditions -> YouTube/Instagram.

Layering, outermost first:
    run.py            thin CLI, argument parsing only
    engine/sources    capture:   pull items + media from a platform
    engine/content    transform: select, dedupe, render for a target format
    engine/destinations publish: push a rendition to a channel/account
    engine/db, config  durable state (Supabase) and configuration

Nothing durable is ever written to local disk, so this machine, the EC2 and an
ephemeral 6-hour runner are interchangeable.
"""

__version__ = "0.2.0"
