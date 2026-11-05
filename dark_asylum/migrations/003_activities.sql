-- Clan activities: events, competitions, giveaways, surveys.
-- All timestamps are UTC ISO-8601 strings ("YYYY-MM-DDTHH:MM:SSZ").

-- Scheduled clan events. `recurrence_days` > 0 makes the event repeat every N days.
CREATE TABLE events (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    starts_at       TEXT NOT NULL,
    recurrence_days INTEGER NOT NULL DEFAULT 0,
    remind_24h      INTEGER NOT NULL DEFAULT 1,   -- per-event reminder switches
    remind_1h       INTEGER NOT NULL DEFAULT 1,
    announce_channel_id TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'scheduled',   -- scheduled | cancelled
    created_by      INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
-- One row per reminder actually sent, so a restart never re-sends it.
CREATE TABLE event_reminders (
    event_id   INTEGER NOT NULL REFERENCES events(id),
    occurrence TEXT NOT NULL,       -- the start time this reminder was for
    kind       TEXT NOT NULL,       -- 24h | 1h
    sent_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (event_id, occurrence, kind)
);
-- Attendance counts only once an admin confirms it; points are paid once.
CREATE TABLE event_attendance (
    event_id    INTEGER NOT NULL REFERENCES events(id),
    occurrence  TEXT NOT NULL,
    discord_id  INTEGER NOT NULL,
    confirmed_by INTEGER NOT NULL,
    confirmed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (event_id, occurrence, discord_id)
);

-- Competitions (individuals only). Lifecycle: draft -> open -> review -> finished (or cancelled).
CREATE TABLE competitions (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL,                 -- xp | proof
    skill       TEXT,                          -- the single skill for XP contests
    rewarded    INTEGER NOT NULL DEFAULT 0,    -- 1 = prize contest: members only
    status      TEXT NOT NULL DEFAULT 'draft',
    created_by  INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE competition_entries (
    id             INTEGER PRIMARY KEY,
    competition_id INTEGER NOT NULL REFERENCES competitions(id),
    discord_id     INTEGER NOT NULL,
    label          TEXT NOT NULL,              -- RSN or Discord name shown on results
    is_member      INTEGER NOT NULL,
    proof          TEXT,                       -- link/description for proof contests
    proof_status   TEXT NOT NULL DEFAULT 'none',   -- none | pending | approved | rejected
    placement      INTEGER,                    -- set by the admin at review
    UNIQUE (competition_id, discord_id)
);
-- Daily XP snapshots (groundwork: filled only when a verified XP provider exists).
CREATE TABLE competition_snapshots (
    competition_id INTEGER NOT NULL REFERENCES competitions(id),
    discord_id     INTEGER NOT NULL,
    day            TEXT NOT NULL,
    xp             INTEGER NOT NULL,
    PRIMARY KEY (competition_id, discord_id, day)
);

-- Giveaways: paid entries (max 5 incl. 1 free), points spent immediately.
CREATE TABLE giveaways (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    prize_gp     INTEGER NOT NULL,             -- prize value paid from the bank
    entry_cost   INTEGER NOT NULL,             -- points per paid entry
    status       TEXT NOT NULL DEFAULT 'open', -- open | drawn | cancelled
    winner_id    INTEGER,
    payout_paid  INTEGER NOT NULL DEFAULT 0,
    channel_id   TEXT NOT NULL DEFAULT '',
    announced    INTEGER NOT NULL DEFAULT 0,
    created_by   INTEGER NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE giveaway_entries (
    id          INTEGER PRIMARY KEY,
    giveaway_id INTEGER NOT NULL REFERENCES giveaways(id),
    discord_id  INTEGER NOT NULL,
    paid        INTEGER NOT NULL,              -- 0 for the free entry
    points_spent INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Surveys: members only, admin closes manually, answers may be changed while open.
CREATE TABLE surveys (
    id         INTEGER PRIMARY KEY,
    question   TEXT NOT NULL,
    anonymous  INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'open',   -- open | closed
    channel_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',       -- Discord message carrying the buttons
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE survey_options (
    id        INTEGER PRIMARY KEY,
    survey_id INTEGER NOT NULL REFERENCES surveys(id),
    label     TEXT NOT NULL
);
-- Identity is stored in `voter_ref`: the Discord ID for named surveys, and a
-- salted one-way hash for anonymous ones, so portal queries cannot reveal voters.
CREATE TABLE survey_votes (
    survey_id INTEGER NOT NULL REFERENCES surveys(id),
    voter_ref TEXT NOT NULL,
    option_id INTEGER NOT NULL REFERENCES survey_options(id),
    PRIMARY KEY (survey_id, voter_ref)
);

INSERT INTO settings(key, value) VALUES ('events_channel_id', '');
INSERT INTO settings(key, value) VALUES ('giveaway_max_entries', '5');
