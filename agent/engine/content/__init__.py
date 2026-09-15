"""Transform side: items -> renditions.

An item is the raw captured message. A rendition is that item reworked for ONE
platform format (a YouTube Short, an Instagram Reel, a carousel, a text post).
They are separate tables so a single source message can feed several channels
without being re-fetched, and a failed publish never forces a re-render.

Planned stages, in order:
  select   score/filter items (recency, engagement, media presence, LLM gate)
  dedupe   collapse items sharing content_hash — the same forward hits many channels
  group    fold a Telegram album (shared grouped_id) into one multi-media post
  render   build the artifact: caption text, 9:16 crop, burned-in subtitles
  approve  optional human gate before anything goes live

Not implemented yet. The NVIDIA NIM / Z.ai keys already in CREDENTIALS.env are the
intended LLM path for `select` and the caption half of `render`.
"""
