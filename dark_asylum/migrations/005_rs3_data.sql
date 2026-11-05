-- Data pulled from the public RS3 endpoints (see rs3api.py / rs3sync.py).

-- Whether a member's RuneMetrics profile is private (Citadel auto-tracking impossible -> manual check).
ALTER TABLE members ADD COLUMN profile_private INTEGER NOT NULL DEFAULT 0;
ALTER TABLE members ADD COLUMN profile_checked_at TEXT;

-- Latest public clan roster (replaced on every sync). rsn_key is the normalised RSN.
CREATE TABLE clan_roster (
    rsn_key    TEXT PRIMARY KEY,
    rsn        TEXT NOT NULL,
    rank       TEXT NOT NULL,
    total_xp   INTEGER NOT NULL,        -- XP earned since the player joined the clan (as served)
    kills      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- One roster-XP data point per player per UTC day.
CREATE TABLE clan_xp_snapshots (
    rsn_key TEXT NOT NULL,
    day     TEXT NOT NULL,
    xp      INTEGER NOT NULL,
    PRIMARY KEY (rsn_key, day)
);

-- Per-skill XP snapshots of each active member ('Overall' is the total), one set per sync pass
-- (default every 6 hours). taken_at is a UTC ISO timestamp, so periods can use boundary times.
CREATE TABLE xp_snapshots (
    discord_id INTEGER NOT NULL,
    taken_at   TEXT NOT NULL,
    skill      TEXT NOT NULL,
    xp         INTEGER NOT NULL,
    PRIMARY KEY (discord_id, taken_at, skill)
);

-- Adventure-log lines already processed, so a line is never applied twice (dedupe key).
CREATE TABLE runemetrics_seen (
    discord_id    INTEGER NOT NULL,
    activity_date TEXT NOT NULL,
    text          TEXT NOT NULL,
    applied       INTEGER NOT NULL DEFAULT 0,
    seen_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (discord_id, activity_date, text)
);

-- Set when the end-of-contest snapshot has been taken (cleared if the contest reopens).
ALTER TABLE competitions ADD COLUMN final_snapshot_at TEXT;
ALTER TABLE competitions ADD COLUMN started_at TEXT;   -- first time the contest opened (UTC ISO)

-- Manually entered double-XP weekends (there is no public feed for them).
CREATE TABLE dxp_periods (
    id         INTEGER PRIMARY KEY,
    starts_on  TEXT NOT NULL,           -- UTC date
    ends_on    TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_by INTEGER NOT NULL
);

-- Sync cadence defaults (editable in the portal's settings; bot restart applies loop-interval changes).
INSERT OR IGNORE INTO settings (key, value) VALUES
    ('xp_snapshot_hours', '6'),
    ('roster_interval_minutes', '60'),
    ('citadel_poll_minutes', '30');
