-- Tickets, reaction roles and Citadel cycle tracking.

-- Staff-configurable ticket categories shown in the Discord dropdown.
CREATE TABLE ticket_types (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1
);
INSERT INTO ticket_types(name, description) VALUES
    ('General support', 'Questions or help from the clan team.'),
    ('Appeal', 'Appeal a moderation decision (permanent bans cannot be appealed).');

-- One row per ticket; type name is copied so renaming a type never rewrites history.
CREATE TABLE tickets (
    id          INTEGER PRIMARY KEY,
    opener_id   INTEGER NOT NULL,
    type_name   TEXT NOT NULL,
    thread_id   INTEGER NOT NULL UNIQUE,
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    claimed_by  INTEGER,                    -- exactly one claimant at a time
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    closed_at   TEXT,
    closed_by   INTEGER,
    close_reason TEXT
);

-- Opener / claim / takeover / close history.
CREATE TABLE ticket_log (
    id        INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    actor_id  INTEGER NOT NULL,
    event     TEXT NOT NULL,
    detail    TEXT
);

-- Index of the locally stored transcript (kept indefinitely). Paths are relative to ARCHIVE_DIR.
CREATE TABLE ticket_archives (
    ticket_id       INTEGER PRIMARY KEY REFERENCES tickets(id),
    transcript_path TEXT NOT NULL,
    message_count   INTEGER NOT NULL,
    dm_status       TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | failed
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE ticket_attachments (
    id            INTEGER PRIMARY KEY,
    ticket_id     INTEGER NOT NULL REFERENCES tickets(id),
    original_name TEXT NOT NULL,
    stored_path   TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL
);

-- Emoji -> role mappings on a chosen message. status is maintained by the bot.
CREATE TABLE reaction_roles (
    id         INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    emoji      TEXT NOT NULL,
    role_id    INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending | ok | blocked
    status_note TEXT,
    UNIQUE (message_id, emoji)
);

-- Citadel cycles: one open cycle at a time; resetting archives it.
CREATE TABLE citadel_cycles (
    id         INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ended_at   TEXT,
    visitors   INTEGER,                           -- frozen totals written on reset
    cappers    INTEGER
);
INSERT INTO citadel_cycles DEFAULT VALUES;

CREATE TABLE citadel_entries (
    cycle_id      INTEGER NOT NULL REFERENCES citadel_cycles(id),
    discord_id    INTEGER NOT NULL,
    visited       INTEGER NOT NULL DEFAULT 0,
    capped        INTEGER NOT NULL DEFAULT 0,
    visit_awarded INTEGER NOT NULL DEFAULT 0,     -- guards against double awards
    cap_awarded   INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (cycle_id, discord_id)
);

INSERT INTO settings(key, value) VALUES ('ticket_channel_id', ''), ('ticket_staff_role', 'Ticket Staff');
