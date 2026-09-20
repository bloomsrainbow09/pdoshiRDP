-- Agent state. Idempotent: safe to re-run (agent/db.py migrate).
--
-- Everything durable lives here because the runner is killed every 6 hours. The three
-- UNIQUE constraints below are the whole reason a wipe is survivable:
--   agent_events   UNIQUE(channel_id, message_id)      -> never process twice
--   notifications  UNIQUE(dedupe_key)                  -> never EMAIL twice
--   agent_state    one row per channel                 -> resume exactly where we stopped
--
-- The cursor semantics matter: last_processed_id is the last message fully processed
-- AND delivered, not merely received. A crash between receipt and delivery must
-- replay, because a lost alert is the failure this system exists to prevent.

CREATE SCHEMA IF NOT EXISTS content;

-- ------------------------------------------------------------------ events ----
-- The typed event stream (Manus pattern). Every message that arrives gets exactly one
-- row and exactly one terminal decision. `decision` is NEVER null for a finished row —
-- that invariant is what makes "nothing is silently dropped" checkable with a query.
CREATE TABLE IF NOT EXISTS content.agent_events (
    id              bigserial PRIMARY KEY,
    channel_id      bigint      NOT NULL,
    message_id      bigint      NOT NULL,
    channel_title   text,
    tier            text,
    posted_at       timestamptz,
    received_at     timestamptz NOT NULL DEFAULT now(),
    text            text,
    urls            text[],
    has_media       boolean     NOT NULL DEFAULT false,
    media_kind      text,
    content_hash    text,

    -- classification
    intent          text,
    confidence      numeric,
    router_model    text,
    reasoning       text,
    entities        jsonb,

    -- outcome. decision ∈ notify | digest | discard | escalate | failed
    decision        text,
    decision_reason text,           -- REQUIRED for every discard. No silent drops.
    decided_at      timestamptz,

    status          text        NOT NULL DEFAULT 'received',
    attempts        int         NOT NULL DEFAULT 0,
    raw             jsonb,
    UNIQUE (channel_id, message_id)
);
CREATE INDEX IF NOT EXISTS agent_events_status_idx  ON content.agent_events (status);
CREATE INDEX IF NOT EXISTS agent_events_intent_idx  ON content.agent_events (intent);
CREATE INDEX IF NOT EXISTS agent_events_recv_idx    ON content.agent_events (received_at DESC);
CREATE INDEX IF NOT EXISTS agent_events_hash_idx    ON content.agent_events (content_hash);
-- Anything received but not yet decided. Should be empty at rest; a non-empty result
-- after a restart is exactly the set to replay.
CREATE INDEX IF NOT EXISTS agent_events_pending_idx ON content.agent_events (id)
    WHERE decision IS NULL;

-- -------------------------------------------------------------------- runs ----
-- One row per model call. Cost visibility is not optional for something that runs
-- unattended for weeks.
CREATE TABLE IF NOT EXISTS content.agent_runs (
    id              bigserial PRIMARY KEY,
    event_id        bigint REFERENCES content.agent_events(id) ON DELETE CASCADE,
    role            text        NOT NULL,      -- guard | router | analyst | vision | writer
    provider        text,
    model           text,
    ok              boolean     NOT NULL,
    latency_ms      int,
    prompt_tokens   int         NOT NULL DEFAULT 0,
    completion_tokens int       NOT NULL DEFAULT 0,
    est_cost_usd    numeric(12, 8) NOT NULL DEFAULT 0,
    attempts        jsonb,                      -- the fallback chain actually walked
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_runs_created_idx ON content.agent_runs (created_at DESC);
CREATE INDEX IF NOT EXISTS agent_runs_role_idx    ON content.agent_runs (role, ok);

-- ----------------------------------------------------------- notifications ----
-- UNIQUE(dedupe_key) is the single most important constraint in this file. The same
-- IPO is announced by six channels within minutes (49% of texted posts in the archive
-- are duplicates), and a runner dying between SMTP-send and mark-sent must not resend.
CREATE TABLE IF NOT EXISTS content.notifications (
    id              bigserial PRIMARY KEY,
    dedupe_key      text        NOT NULL UNIQUE,
    event_id        bigint REFERENCES content.agent_events(id) ON DELETE SET NULL,
    intent          text,
    channel_kind    text        NOT NULL DEFAULT 'email',   -- email | whatsapp | telegram
    recipient       text        NOT NULL,
    subject         text,
    body_text       text,
    body_html       text,
    template        text,
    status          text        NOT NULL DEFAULT 'queued',  -- queued|sent|failed|suppressed
    suppressed_why  text,                                    -- rate limit | quiet hours | duplicate
    scheduled_for   timestamptz,
    sent_at         timestamptz,
    attempts        int         NOT NULL DEFAULT 0,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notifications_status_idx ON content.notifications (status, scheduled_for);
CREATE INDEX IF NOT EXISTS notifications_sent_idx   ON content.notifications (sent_at DESC);

-- ------------------------------------------------------------------- state ----
-- The resume point. One row per channel; plus singleton rows for the watcher lease.
CREATE TABLE IF NOT EXISTS content.agent_state (
    key                 text PRIMARY KEY,       -- 'channel:<id>' | 'watcher:<account>'
    channel_id          bigint,
    last_processed_id   bigint      NOT NULL DEFAULT 0,
    last_seen_at        timestamptz,
    heartbeat_at        timestamptz,
    worker_id           text,
    lease_until         timestamptz,            -- watcher lease; see the note below
    meta                jsonb,
    updated_at          timestamptz NOT NULL DEFAULT now()
);
COMMENT ON COLUMN content.agent_state.lease_until IS
  'Single-watcher lease. Two Telegram clients sharing one session can get the auth key
   REVOKED, which needs a human with a phone and would end the zero-human guarantee.
   The 6-hour runner handoff overlaps by ~5 minutes, which is exactly that hazard, so a
   watcher must hold an unexpired lease to run.';

-- ------------------------------------------------------------------ errors ----
-- Dead-letter. Anything that fails 3x lands here with full context and is EMAILED,
-- never silently dropped.
CREATE TABLE IF NOT EXISTS content.agent_errors (
    id              bigserial PRIMARY KEY,
    event_id        bigint REFERENCES content.agent_events(id) ON DELETE SET NULL,
    stage           text,                        -- watcher|guard|router|subagent|compose|deliver
    error_class     text,
    message         text,
    context         jsonb,
    attempts        int         NOT NULL DEFAULT 0,
    resolved        boolean     NOT NULL DEFAULT false,
    alerted         boolean     NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_errors_open_idx ON content.agent_errors (created_at DESC)
    WHERE NOT resolved;

-- ------------------------------------------------------------- trade calls ----
-- The accuracy ledger. Every extracted call is stored so per-channel quality stops
-- being a one-off hand audit and becomes a measurement that keeps running.
--
-- `has_stop_loss` is the column that earns its keep. The audit behind tiers.json found
-- the stop-loss rate ranging from 80% on `Trading bull stock` to near zero elsewhere,
-- and it is the strongest single quality marker in the corpus — a channel that never
-- states an exit is not making a falsifiable claim. Storing it per call is how that
-- number stays current instead of ageing into folklore.
CREATE TABLE IF NOT EXISTS content.trade_calls (
    id              bigserial PRIMARY KEY,
    event_id        bigint UNIQUE REFERENCES content.agent_events(id) ON DELETE CASCADE,
    channel_id      bigint,
    channel_title   text,
    tier            text,
    posted_at       timestamptz,
    symbol          text,
    direction       text,                        -- buy | sell | null
    instrument      text,                        -- equity | futures | options | index
    entry           text,                        -- kept as text: sources write ranges
    target          text,
    target_2        text,
    stop_loss       text,
    has_stop_loss   boolean     NOT NULL DEFAULT false,
    timeframe       text,
    outcome         text,                        -- hit | stopped | expired | null (P11+)
    outcome_at      timestamptz,
    raw_text        text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS trade_calls_channel_idx ON content.trade_calls (channel_id, posted_at DESC);
CREATE INDEX IF NOT EXISTS trade_calls_symbol_idx  ON content.trade_calls (symbol);
COMMENT ON COLUMN content.trade_calls.entry IS
  'Text, not numeric, and deliberately so: real posts write "1240-1250", "abv 1250",
   "cmp". Parsing that into a number at write time would silently discard the range.';

-- The sub-agent's structured result, kept on the event so P8 can compose an email from
-- facts rather than re-deriving them from the raw text with another model call.
ALTER TABLE content.agent_events ADD COLUMN IF NOT EXISTS agent_outcome jsonb;

-- ------------------------------------------------------- multi-vertical capture ----
-- Which vertical owns this event.
--
-- Only ONE client may hold a Telegram session's lease — two clients on one session from
-- two IPs is AuthKeyDuplicatedError, permanent, and recovering it needs a human with a
-- phone. So several verticals sharing an account cannot each run their own watcher;
-- they have to share one, and the drain then needs to know which plugin owns each row.
--
-- NULL means "written before this column existed", and `orchestrator.ctx(None)` resolves
-- that to the active vertical — exactly the behaviour those rows were processed with.
ALTER TABLE content.agent_events ADD COLUMN IF NOT EXISTS vertical text;
CREATE INDEX IF NOT EXISTS agent_events_vertical_idx ON content.agent_events (vertical);
-- The drain's hot query: undecided rows, optionally for one vertical.
CREATE INDEX IF NOT EXISTS agent_events_pending_vertical_idx
    ON content.agent_events (vertical, id) WHERE decision IS NULL;
