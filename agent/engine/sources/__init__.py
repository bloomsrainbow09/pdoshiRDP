"""Capture side: anything that produces content.items rows.

Telegram is the only source today. A new one (an RSS feed, a subreddit, another
Telegram account pool) needs to do just two things to fit:
  - register its channels in content.source_channels
  - insert rows into content.items, keyed (channel_id, message_id) for idempotency
Everything downstream — dedupe, rendering, scheduling, publishing — is unchanged.
"""
