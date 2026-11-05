-- Initial schema shared by the bot and the portal.
-- Discord IDs (INTEGER) are the stable identity key everywhere; names are display-only.

-- Key/value configuration edited from the portal and read by the bot.
CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Portal administrators (at most the two designated Discord IDs).
CREATE TABLE admins (
    id              INTEGER PRIMARY KEY,
    discord_id      INTEGER NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    totp_secret_enc TEXT NOT NULL,          -- Fernet-encrypted TOTP secret
    totp_confirmed  INTEGER NOT NULL DEFAULT 0,
    last_totp_step  INTEGER NOT NULL DEFAULT 0,  -- blocks OTP replay
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Single-use MFA backup codes (only salted hashes are stored).
CREATE TABLE backup_codes (
    id        INTEGER PRIMARY KEY,
    admin_id  INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    used_at   TEXT
);

-- One-time tokens that start (or recover) an admin account.
CREATE TABLE setup_tokens (
    id         INTEGER PRIMARY KEY,
    discord_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);

-- Clan members, keyed by Discord ID. Rows are never deleted (history is retained).
CREATE TABLE members (
    discord_id      INTEGER PRIMARY KEY,
    rsn             TEXT COLLATE NOCASE,   -- current RuneScape 3 name only (no aliases kept)
    join_date       TEXT,                  -- ISO date
    timezone        TEXT,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','left')),
    ranking_points  INTEGER NOT NULL DEFAULT 0 CHECK (ranking_points >= 0),
    spending_points INTEGER NOT NULL DEFAULT 0 CHECK (spending_points >= 0),
    ranking_opt_out INTEGER NOT NULL DEFAULT 0,  -- admin-only flag
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
-- An RSN may belong to only one ACTIVE member at a time.
CREATE UNIQUE INDEX idx_members_rsn ON members(rsn) WHERE rsn IS NOT NULL AND status = 'active';

-- Each stint in the clan; rejoining opens a new period with zero strikes.
CREATE TABLE membership_periods (
    id         INTEGER PRIMARY KEY,
    discord_id INTEGER NOT NULL REFERENCES members(discord_id),
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ended_at   TEXT,
    end_reason TEXT
);

-- Warning strikes; strikes are counted per membership period.
CREATE TABLE warnings (
    id            INTEGER PRIMARY KEY,
    discord_id    INTEGER NOT NULL,
    period_id     INTEGER NOT NULL REFERENCES membership_periods(id),
    strike_number INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    issued_by     INTEGER NOT NULL,        -- Discord ID of the leader
    action_taken  TEXT NOT NULL,
    dm_delivered  INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Immutable GP bank ledger. balance_after can never be negative.
CREATE TABLE bank_ledger (
    id            INTEGER PRIMARY KEY,
    amount_gp     INTEGER NOT NULL CHECK (amount_gp <> 0),
    balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
    reason        TEXT NOT NULL,
    actor_id      INTEGER NOT NULL,        -- Discord ID of the portal admin
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Immutable points ledger; ranking and spending balances move separately.
CREATE TABLE points_ledger (
    id            INTEGER PRIMARY KEY,
    discord_id    INTEGER NOT NULL REFERENCES members(discord_id),
    period_id     INTEGER REFERENCES membership_periods(id),
    ranking_delta INTEGER NOT NULL DEFAULT 0,
    spending_delta INTEGER NOT NULL DEFAULT 0,
    kind          TEXT NOT NULL,           -- award | spend | forfeit | adjustment
    reason        TEXT NOT NULL,
    actor_id      INTEGER,                 -- NULL for automatic awards
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Admin-defined preset awards (also what !point_values prints).
CREATE TABLE point_awards (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL UNIQUE,
    amount INTEGER NOT NULL CHECK (amount > 0)
);

-- Saved text responses invoked with the custom prefix.
CREATE TABLE custom_commands (
    name     TEXT PRIMARY KEY COLLATE NOCASE,
    response TEXT NOT NULL,
    enabled  INTEGER NOT NULL DEFAULT 1
);

-- Roles granted for a limited time; the bot removes them on expiry.
CREATE TABLE temp_roles (
    id         INTEGER PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    discord_id INTEGER NOT NULL,
    role_id    INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    granted_by INTEGER NOT NULL,
    removed_at TEXT
);

-- Important-action audit log (90 day retention). Never holds deleted message bodies.
CREATE TABLE audit_log (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    source     TEXT NOT NULL,              -- portal | bot | system
    actor_id   INTEGER,
    category   TEXT NOT NULL,
    action     TEXT NOT NULL,
    target     TEXT,
    before     TEXT,
    after      TEXT,
    detail     TEXT
);
CREATE INDEX idx_audit_created ON audit_log(created_at);

-- Liveness markers written by the bot and the backup job (read by the health page).
CREATE TABLE heartbeats (
    name       TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'ok',
    detail     TEXT
);

-- Seed values straight from the clan's point table.
INSERT INTO point_awards (name, amount) VALUES
    ('Citadel visit', 50),
    ('Citadel cap bonus', 100),
    ('Clan event attendance', 150),
    ('Competition participation', 100),
    ('Placement bonus: 1st', 200),
    ('Placement bonus: 2nd', 100),
    ('Placement bonus: 3rd', 50);

INSERT INTO settings (key, value) VALUES
    ('prefix', '!'),
    ('cprefix', '!'),
    ('clan_name', 'Dark Asylum'),
    ('member_role', 'Member'),
    ('sr_role', 'Sr_Leadership'),
    ('jr_role', 'Jr_leadership'),
    ('command_channel_id', ''),
    ('staff_channel_id', ''),
    ('ranking_target_points', '0'),
    ('points_to_gp', '100000');
