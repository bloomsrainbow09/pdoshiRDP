-- Content engine schema. Idempotent: safe to re-run (engine/db.py migrate).
--
-- Own schema rather than a corner of `n8n`, because this grows to ~7 tables and
-- n8n owns 93 in there already. NOTE: server-backup.sh dumps only `n8n` and
-- `reddit_miner`, so add `--schema=content` to it or this is covered by the
-- daily supabase-backup alone.
--
-- Shape:  source_accounts -> source_channels -> items -> media
--                                                 |
--                                             renditions -> posts -> destinations
--
-- An item is one captured Telegram message. A rendition is that item reworked for
-- one platform (a Short, a Reel, a carousel). A post is one publish attempt of one
-- rendition to one destination account. Keeping them separate means the same source
-- message can feed several channels without being re-fetched or re-rendered.

CREATE SCHEMA IF NOT EXISTS content;

-- ---------------------------------------------------------------- sources ----

-- Telegram (today) and whatever else later. A session_string is FULL control of
-- that account -- not merely API access.
CREATE TABLE IF NOT EXISTS content.source_accounts (
    label           text PRIMARY KEY,
    platform        text    NOT NULL DEFAULT 'telegram',
    phone           text,
    api_id          bigint  NOT NULL,
    api_hash        text    NOT NULL,
    session_string  text,
    user_id         bigint,
    username        text,
    first_name      text,
    is_active       boolean NOT NULL DEFAULT true,
    last_login_at   timestamptz,
    last_used_at    timestamptz,
    note            text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- One row per channel we pull from. `folder` mirrors the Telegram chat folder
-- ("Trading", "Dealdost"), which is the natural grouping to drive a destination.
CREATE TABLE IF NOT EXISTS content.source_channels (
    id              bigint  PRIMARY KEY,          -- Telegram peer id, stable
    account_label   text    NOT NULL REFERENCES content.source_accounts(label) ON DELETE CASCADE,
    title           text,
    username        text,
    kind            text,                          -- channel | group | user
    folder          text,
    is_enabled      boolean NOT NULL DEFAULT true,
    last_message_id bigint  NOT NULL DEFAULT 0,    -- resume point; replaces the local json
    last_synced_at  timestamptz,
    note            text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS source_channels_folder_idx ON content.source_channels (folder);

-- ------------------------------------------------------------------ items ----

-- One captured message. (channel_id, message_id) is the idempotency key: re-running
-- ingest never duplicates, which is what makes a 6-hour runner wipe harmless.
CREATE TABLE IF NOT EXISTS content.items (
    id              bigserial PRIMARY KEY,
    channel_id      bigint  NOT NULL REFERENCES content.source_channels(id) ON DELETE CASCADE,
    message_id      bigint  NOT NULL,
    posted_at       timestamptz,
    text            text,
    urls            text[],
    views           bigint,
    forwards        bigint,
    replies         bigint,
    grouped_id      bigint,                        -- Telegram album: several messages, one post
    reply_to        bigint,
    has_media       boolean NOT NULL DEFAULT false,
    content_hash    text,                          -- for cross-channel dedupe (same forward everywhere)
    status          text    NOT NULL DEFAULT 'new',-- new | selected | rejected | rendered
    score           numeric,                       -- optional LLM/heuristic ranking
    tags            text[],
    raw             jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (channel_id, message_id)
);
CREATE INDEX IF NOT EXISTS items_status_idx       ON content.items (status);
CREATE INDEX IF NOT EXISTS items_posted_at_idx    ON content.items (posted_at DESC);
CREATE INDEX IF NOT EXISTS items_content_hash_idx ON content.items (content_hash);

-- Media never goes in the database: the EC2 has an 8 GB disk and runners are
-- ephemeral, so bytes live in object storage and this table holds the pointer.
CREATE TABLE IF NOT EXISTS content.media (
    id              bigserial PRIMARY KEY,
    item_id         bigint  NOT NULL REFERENCES content.items(id) ON DELETE CASCADE,
    kind            text,                          -- photo | video | document | audio
    mime            text,
    bytes           bigint,
    width           int,
    height          int,
    duration_s      numeric,
    storage         text,                          -- gdrive | supabase | local
    storage_ref     text,                          -- file id / object key / path
    sha256          text,
    downloaded_at   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS media_item_idx   ON content.media (item_id);
CREATE INDEX IF NOT EXISTS media_sha256_idx ON content.media (sha256);

-- ----------------------------------------------------------- destinations ----

-- A YouTube channel or Instagram account we publish to. Credentials differ wildly
-- per platform (YouTube = OAuth refresh token, Instagram = IG Graph long-lived
-- token + business account id), so they live in `credentials` jsonb rather than
-- forcing one column shape onto both.
CREATE TABLE IF NOT EXISTS content.destinations (
    label           text PRIMARY KEY,
    platform        text    NOT NULL,              -- youtube | instagram
    display_name    text,
    external_id     text,                          -- channel id / ig user id
    credentials     jsonb,
    defaults        jsonb,                         -- title/description templates, tags, privacy
    source_folder   text,                          -- which Telegram folder feeds it
    is_active       boolean NOT NULL DEFAULT true,
    daily_cap       int     NOT NULL DEFAULT 5,
    last_posted_at  timestamptz,
    note            text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------- renditions and posting ----

-- An item reworked for one platform: the caption, the cut, the aspect ratio.
-- Kept apart from `posts` so one rendition can go to several destinations, and a
-- failed publish never means re-rendering.
CREATE TABLE IF NOT EXISTS content.renditions (
    id              bigserial PRIMARY KEY,
    item_id         bigint  NOT NULL REFERENCES content.items(id) ON DELETE CASCADE,
    format          text    NOT NULL,              -- short | reel | carousel | story | text
    title           text,
    caption         text,
    hashtags        text[],
    storage         text,
    storage_ref     text,                          -- the rendered file, if any
    duration_s      numeric,
    status          text    NOT NULL DEFAULT 'pending', -- pending | ready | failed
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (item_id, format)
);
CREATE INDEX IF NOT EXISTS renditions_status_idx ON content.renditions (status);

-- One publish attempt. UNIQUE(destination, rendition) is the safety rail: a retry
-- after a timeout can never double-post to the same channel.
CREATE TABLE IF NOT EXISTS content.posts (
    id                bigserial PRIMARY KEY,
    rendition_id      bigint  NOT NULL REFERENCES content.renditions(id) ON DELETE CASCADE,
    destination_label text    NOT NULL REFERENCES content.destinations(label) ON DELETE CASCADE,
    status            text    NOT NULL DEFAULT 'queued', -- queued | publishing | live | failed | skipped
    scheduled_for     timestamptz,
    published_at      timestamptz,
    external_id       text,                        -- video id / media id
    external_url      text,
    attempts          int     NOT NULL DEFAULT 0,
    error             text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (destination_label, rendition_id)
);
CREATE INDEX IF NOT EXISTS posts_status_sched_idx ON content.posts (status, scheduled_for);

-- --------------------------------------------------------------- verticals ---

-- A niche: which channels feed it, how its posts look, where they go. The source
-- of truth is verticals/<name>/vertical.json, edited by hand; `run.py vertical
-- sync` mirrors it here so a wiped runner reads config from the database and never
-- needs the folder to make decisions.
CREATE TABLE IF NOT EXISTS content.verticals (
    name        text PRIMARY KEY,
    display_name text,
    is_enabled  boolean NOT NULL DEFAULT true,
    config      jsonb   NOT NULL,
    synced_at   timestamptz NOT NULL DEFAULT now(),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------- OCR ----
-- Most of these channels post chart/call SCREENSHOTS, not text: ~26% of messages
-- are photos and the median caption is 3-5 characters. The substance is inside the
-- image, so OCR is not a nicety -- without it the pipeline has almost nothing to
-- work with.
--
-- OCR is also what makes the media ephemeral. Once the text is extracted the image
-- has served its purpose, so the bytes are deleted the next day while ocr_text and
-- ocr_json are kept forever. Trading images are worthless after their session; the
-- numbers in them are not.
ALTER TABLE content.media ADD COLUMN IF NOT EXISTS ocr_text   text;
ALTER TABLE content.media ADD COLUMN IF NOT EXISTS ocr_json   jsonb;
ALTER TABLE content.media ADD COLUMN IF NOT EXISTS ocr_model  text;
ALTER TABLE content.media ADD COLUMN IF NOT EXISTS ocr_at     timestamptz;
ALTER TABLE content.media ADD COLUMN IF NOT EXISTS ocr_error  text;
ALTER TABLE content.media ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
CREATE INDEX IF NOT EXISTS media_needs_ocr_idx ON content.media (id)
    WHERE ocr_at IS NULL AND deleted_at IS NULL;
