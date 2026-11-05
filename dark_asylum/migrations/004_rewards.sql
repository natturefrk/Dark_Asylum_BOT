-- XP milestones, redemption windows, Bossing Gear goals, message-purge jobs.

-- Milestone definitions. scope 'skill' = per-member per-skill threshold;
-- scope 'clan' = total clan XP threshold (paid once to every active member).
CREATE TABLE xp_milestones (
    id          INTEGER PRIMARY KEY,
    scope       TEXT NOT NULL CHECK (scope IN ('skill','clan')),
    skill       TEXT,                       -- NULL for clan milestones
    threshold   INTEGER NOT NULL CHECK (threshold > 0),
    points      INTEGER NOT NULL CHECK (points > 0),
    enabled     INTEGER NOT NULL DEFAULT 1,
    UNIQUE (scope, skill, threshold)
);
-- One row per (milestone, member): the primary key is what makes awards once-only.
-- Clan milestones store discord_id 0 as the "reached" marker plus one row per paid member.
CREATE TABLE xp_milestone_awards (
    milestone_id INTEGER NOT NULL REFERENCES xp_milestones(id),
    discord_id   INTEGER NOT NULL,
    awarded_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (milestone_id, discord_id)
);
INSERT INTO xp_milestones(scope, skill, threshold, points) VALUES
    ('skill', '*', 13034431, 250),      -- '*' = any skill reaching level 99
    ('skill', '*', 36073511, 500),      -- level 120
    ('skill', '*', 200000000, 1000);    -- 200m XP

-- Admin-scheduled single-day redemption windows (two per month: mid-month and month-end).
CREATE TABLE redemption_windows (
    id     INTEGER PRIMARY KEY,
    month  TEXT NOT NULL,                  -- YYYY-MM (UTC)
    kind   TEXT NOT NULL CHECK (kind IN ('mid','end')),
    day    TEXT NOT NULL,                  -- YYYY-MM-DD (UTC)
    UNIQUE (month, kind)
);
-- A request is a GP or item payout bought with spending points.
CREATE TABLE redemptions (
    id          INTEGER PRIMARY KEY,
    discord_id  INTEGER NOT NULL,
    window_id   INTEGER NOT NULL REFERENCES redemption_windows(id),
    month       TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('gp','item')),
    item_name   TEXT,
    gp_value    INTEGER NOT NULL,
    points_cost INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'requested',  -- requested | approved | paid | rejected | cancelled
    decided_by  INTEGER,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
-- The monthly slot is taken on approval: at most one live redemption per member per month.
CREATE UNIQUE INDEX one_redemption_per_month ON redemptions(discord_id, month)
    WHERE status IN ('approved','paid');

-- Bossing Gear catalog (price reference + admin confirmation/override).
CREATE TABLE bossing_items (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    price_gp     INTEGER NOT NULL CHECK (price_gp > 0),
    price_source TEXT NOT NULL DEFAULT 'manual',   -- manual | ge_reference
    confirmed    INTEGER NOT NULL DEFAULT 0,       -- admin confirmed the price
    enabled      INTEGER NOT NULL DEFAULT 1
);
-- Per-member total discount that REPLACES the default (max 50%).
CREATE TABLE member_discounts (
    discord_id INTEGER PRIMARY KEY,
    percent    INTEGER NOT NULL CHECK (percent BETWEEN 0 AND 50)
);
CREATE TABLE bossing_goals (
    id            INTEGER PRIMARY KEY,
    discord_id    INTEGER NOT NULL,
    item_id       INTEGER NOT NULL REFERENCES bossing_items(id),
    item_name     TEXT NOT NULL,
    catalog_price INTEGER NOT NULL,        -- full value, paid out on fulfilment
    discount_pct  INTEGER NOT NULL,        -- locked when the goal starts
    target_gp     INTEGER NOT NULL,        -- discounted price the member must contribute
    contributed_gp INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active',   -- active | ready | fulfilled | cancelled
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
-- One open goal (active or ready) per member.
CREATE UNIQUE INDEX one_open_goal ON bossing_goals(discord_id) WHERE status IN ('active','ready');
CREATE TABLE goal_contributions (
    id        INTEGER PRIMARY KEY,
    goal_id   INTEGER NOT NULL REFERENCES bossing_goals(id),
    amount_gp INTEGER NOT NULL,
    actor_id  INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Admin-queued message deletions; the bot runs them with its own permission checks.
-- Only metadata is kept (never message bodies).
CREATE TABLE purge_jobs (
    id          INTEGER PRIMARY KEY,
    channel_id  INTEGER NOT NULL,
    days        INTEGER NOT NULL CHECK (days IN (30,60,90)),
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | done | failed
    deleted     INTEGER,
    note        TEXT,
    requested_by INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

INSERT INTO settings(key, value) VALUES ('gear_default_discount', '10');
