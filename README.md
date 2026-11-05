# Dark Asylum – Discord bot + admin portal (RuneScape 3)

A self-hosted Python toolkit: a **discord.py prefix-command bot** and a **secure Flask web portal** sharing one
SQLite database. Settings changed in the portal take effect in the bot immediately.

## Status

| Area | State |
|---|---|
| Portal auth: Argon2id passwords, mandatory TOTP at every login, backup codes, lockout, rate limits, CSRF, strict cookies/CSP, 30-min idle sign-out with 2-min warning, password+MFA confirmation on every change | ✅ done & tested |
| Admin bootstrap/recovery (one-time tokens, two designated Discord IDs only) | ✅ |
| Bank ledger (never negative, actor + reason), points ledger (ranking/spending split, opt-out, forfeit on leave) | ✅ |
| Members (RSN uniqueness, join date, rejoin with zero strikes), strikes 1/2/3 logic | ✅ |
| Bot: `prefix cprefix track track-for join-date clan_name timezone bank points point_values xp cxp kick ban temp_ban warn temp_role` + saved-text custom commands | ✅ implemented; domain logic unit-tested, **not exercised against live Discord** |
| Backups (daily, 30 days, two locations, failure alerts), 90-day audit retention, health page + staff alerts | ✅ |
| RS3 data (roster / clan XP, per-skill hiscores snapshots, Citadel adventure-log polling) via the **unofficial, key-free public Jagex endpoints**; `!xp !cxp !gains !visits !capping !dxp`; portal XP tab (top-10 per day/week/month charts), dashboard cards, rank-promotion view, private-profile flag, API health alerts | ✅ Built and tested against **mocked** HTTP. Live-checked once: roster + hiscores respond. ⚠️ The Citadel log wording ("Visited/Capped … Citadel") is **unverified against a real public profile** (the one tried was private); the matcher is case-insensitive and admins can always correct manually. Endpoints may change without notice. |
| Tickets (dropdown + Submit, private threads, claim/takeover/close, local transcript + attachment archive, DM attempt), reaction roles (portal-configured, privileged roles blocked), Citadel cycles (visit/cap points once per cycle, archived history) | ✅ Built and unit-tested; **not yet run against live Discord** |
| Events (one-time/recurring, 24h/1h reminders, confirmed attendance points), competitions (lifecycle, proof approval, placement/participation awards), giveaways (free first entry, max 5, refunds, random draw, bank payout), surveys (live counts, anonymous option) | ✅ Built and unit-tested; **not yet run against live Discord** |
| XP/clan-XP milestones (configurable, once-only awards), redemption windows (member request, admin approve/pay/cancel, monthly slot, 150m cap), Bossing Gear goals (catalog, discounts, contributions, refund, fulfilment), message deletion by age (30/60/90, permission-checked, counts only audited), per-timezone event reminders | ✅ Built and unit-tested; **not yet run against live Discord**. |
| XP contests | ✅ Baseline = last snapshot at/before opening (≤ one interval of earlier XP may count), final snapshot frozen at review; admin reviews results |

### RS3 sync cadence (all configurable in portal → Moderation → Settings)

| Job | Default | Setting | Notes |
|---|---|---|---|
| XP snapshot pass (hiscores, one request per linked member, serialised ≥1.2 s apart) | **every 6 hours** | `xp_snapshot_hours` (1-24) | Last completed pass is stored (`xp_snapshot_last_run`), so restarts never repeat early. An outage aborts the pass; it is retried within 15 minutes. |
| Clan roster / clan XP | hourly | `roster_interval_minutes` (15-1440) | Needs bot restart to change. |
| Citadel adventure-log poll | every 30 min | `citadel_poll_minutes` (10-120) | Only the latest 20 log lines are served; keep it short so none are missed. Needs restart. |

Day/week/month gains = latest snapshot minus the last snapshot before the period's UTC boundary
(00:00 UTC of the first day); with little history the earliest snapshot is used and the baseline time is shown.
Set `RS3_API_DISABLED=true` to switch all outbound RS3 calls off. The server needs outbound HTTPS to
`secure.runescape.com` and `apps.runescape.com`.

Deviations: leadership targeting another member uses `track-for` (instead of overloading `track`); RSNs containing
spaces must be quoted in commands (`warn "Dark One" reason`).

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env           # fill in secrets (see comments inside)
.venv/bin/python -m dark_asylum.cli init-db
```

### First-time admin setup (safe bootstrap)
No default accounts exist. Only the two Discord IDs in `PORTAL_ADMIN_DISCORD_IDS` can ever own an account.

1. On the server: `python -m dark_asylum.cli setup-token <your-discord-id>` (single-use token, valid 1 hour).
2. Open `https://your-portal/setup`, paste the token, choose a password (14+ chars, mixed).
3. Scan the QR in an authenticator app, enter the code, then **save the 10 backup codes** (shown once).

Recovery: the other admin can issue a recovery token from *Admin Account* (after password + MFA), or the server
operator can run `setup-token` again. Recovery replaces the target's password and authenticator.
Confirmation codes are single-use per 30-second window, so wait for a fresh code when confirming a change right after login.

### Run
```bash
.venv/bin/python -m dark_asylum.cli portal   # portal on PORTAL_HOST:PORTAL_PORT (waitress)
.venv/bin/python -m dark_asylum.cli bot      # Discord bot
.venv/bin/python -m dark_asylum.cli backup   # manual backup (also runs daily from the bot)
```
See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for HTTPS, systemd and backups.

## Discord setup
1. Create the bot in the Discord developer portal; enable **Server Members** and **Message Content** intents.
2. Invite with: Kick Members, Ban Members, Moderate Members, Manage Roles, Send Messages, Read Message History.
3. Roles (names or IDs configurable in the portal *Moderation → Settings*): `Member`, `Sr_Leadership`, `Jr_leadership`.
4. **Role hierarchy:** put the bot's role *above* every role it must moderate/assign. Discord forbids the bot acting
   on anyone at/above its top role. `temp_role` refuses roles with Administrator/Manage Server/Manage Roles, roles
   above the bot, or above the caller.
5. Set a *Member command channel* and *Staff alert channel* in the portal. Members can only run commands in the
   command channel; leadership may run them anywhere. Moderation output is plain text.

Prefix: `!` by default; valid prefixes are `! # \ & $`, changeable only by leadership (`!prefix`, `!cprefix`).

## Data & retention
* One SQLite file (WAL mode) at `DATABASE_PATH`; paths are never displayed in the portal.
* Audit log: important actions only, 90 days (purged daily); settings changes store before/after; deleted message bodies are never stored.
* Bank and points ledgers are append-only. Removing a profile marks it `left`, forfeits points via a ledger row and keeps all history.
* Backups: SQLite online backup, integrity-checked, 30-day retention, written to `BACKUP_DIR` and a **separate**
  `BACKUP_SECONDARY_DIR`. Any failure (including a missing secondary) sets *backup* health to error, writes an audit
  entry and posts to the staff channel.

## Development
```bash
.venv/bin/ruff check . && .venv/bin/pytest -q
```
Put new domain logic in the plain modules (`ledger.py`, `members.py`, `rules.py`) so it stays testable without Discord.
