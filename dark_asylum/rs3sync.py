"""Turns RS3 provider data into database state: roster, XP snapshots, Citadel activity, health.

All functions take the shared SQLite connection and a provider, so they are testable
with a fake provider. They never fabricate data: when the provider is down they record
the outage and return early; a lookup miss for one player only skips that player.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from . import citadel, milestones
from .db import get_setting, heartbeat, iso, now_iso, set_setting, utcnow
from .events import parse_utc
from .members import MemberError
from .rs3api import classify_activity, norm_rsn
from .xp import PlayerNotFound, ProviderUnavailable, XpProvider

# Heartbeat names shown on the health page for each upstream API.
API_ROSTER, API_HISCORES, API_RUNEMETRICS = "rs3_clan_roster", "rs3_hiscores", "rs3_runemetrics"


def record_health(conn: sqlite3.Connection, name: str, ok: bool, detail: str = "") -> str | None:
    """Store an API's up/down state; return an alert text only when the state *changed*.

    The first ever observation of "up" is silent; a transition to down, or a recovery,
    returns a one-line message for the staff channel.
    """
    old = conn.execute("SELECT status FROM heartbeats WHERE name=?", (name,)).fetchone()
    heartbeat(conn, name, "ok" if ok else "down", detail or ("reachable" if ok else "unreachable"))
    before = old["status"] if old else None
    if ok and before == "down":
        return f"RS3 API recovered: {name}"
    if not ok and before != "down":
        return f"RS3 API DOWN: {name} ({detail})"
    return None


def today_str(today: date | None = None) -> str:
    """UTC calendar day used as the snapshot key."""
    return (today or utcnow().date()).isoformat()


# ---- clan roster ------------------------------------------------------


async def sync_roster(conn: sqlite3.Connection, provider: XpProvider, today: date | None = None) -> list[str]:
    """Fetch the roster for ``clan_name``, replace the stored copy, snapshot per-player clan XP.

    Returns staff alerts (API down/recovered). Clan milestones are evaluated on the roster's total.
    """
    clan = get_setting(conn, "clan_name")
    if not clan:
        return []  # nothing to do until an admin runs !clan_name
    alerts: list[str] = []
    try:
        roster = await provider.fetch_roster(clan)
    except ProviderUnavailable as exc:
        return [a for a in [record_health(conn, API_ROSTER, False, str(exc))] if a]
    except PlayerNotFound as exc:
        # The API answered, so it is up; the clan name is simply wrong.
        record_health(conn, API_ROSTER, True, f"clan not found: {exc}")
        return []
    if a := record_health(conn, API_ROSTER, True, f"{len(roster)} clanmates"):
        alerts.append(a)
    day, stamp = today_str(today), now_iso()
    conn.execute("DELETE FROM clan_roster")
    for m in roster:
        key = norm_rsn(m.rsn)
        conn.execute(
            "INSERT OR REPLACE INTO clan_roster(rsn_key,rsn,rank,total_xp,kills,updated_at) VALUES(?,?,?,?,?,?)",
            (key, m.rsn, m.rank, m.total_xp, m.kills, stamp),
        )
        conn.execute("INSERT OR REPLACE INTO clan_xp_snapshots(rsn_key,day,xp) VALUES(?,?,?)", (key, day, m.total_xp))
    alerts += milestones.evaluate_clan(conn, sum(m.total_xp for m in roster))
    return alerts


def clan_xp_for(conn: sqlite3.Connection, rsn: str) -> sqlite3.Row | None:
    """The roster row for an RSN (matching ignores case and space/underscore differences)."""
    return conn.execute("SELECT * FROM clan_roster WHERE rsn_key=?", (norm_rsn(rsn),)).fetchone()


# ---- per-member XP snapshots -----------------------------------------


def interval(conn: sqlite3.Connection, key: str, default: int, low: int, high: int) -> int:
    """A configurable whole-number interval setting, clamped to a sane range (bad values -> default)."""
    raw = get_setting(conn, key, str(default))
    value = int(raw) if raw.isdigit() else default
    return max(low, min(high, value))


def snapshot_hours(conn: sqlite3.Connection) -> int:
    """Hours between XP snapshot passes (setting ``xp_snapshot_hours``; default 6, allowed 1-24)."""
    return interval(conn, "xp_snapshot_hours", 6, 1, 24)


def active_members(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Active members who have an RSN linked (the only ones we can look up)."""
    return conn.execute("SELECT * FROM members WHERE status='active' AND rsn IS NOT NULL").fetchall()


def store_snapshot(conn: sqlite3.Connection, discord_id: int, taken_at: str, skills: dict[str, int]) -> None:
    """Persist one pass's per-skill XP (including Overall) for a member at ``taken_at``."""
    conn.executemany(
        "INSERT OR REPLACE INTO xp_snapshots(discord_id,taken_at,skill,xp) VALUES(?,?,?,?)",
        [(discord_id, taken_at, skill, xp) for skill, xp in skills.items()],
    )


async def sync_xp(conn: sqlite3.Connection, provider: XpProvider, now: datetime | None = None) -> list[str]:
    """Run one XP snapshot pass if the configured interval has elapsed since the last completed pass.

    The last completed run is persisted (``xp_snapshot_last_run``) so restarts never repeat a pass
    early. Requests are serialised and spaced by the provider's fetcher (rate limiting), which also
    staggers a large clan across several minutes. On an outage the pass stops and is NOT recorded as
    complete; the members already captured are skipped on the retry (they have a fresh snapshot).
    Milestones are evaluated for each captured member.
    """
    now = now or utcnow()
    hours = snapshot_hours(conn)
    last = get_setting(conn, "xp_snapshot_last_run")
    if last and now - parse_utc(last) < timedelta(hours=hours):
        return []
    fresh_after, taken_at, alerts, ok_any = iso(now - timedelta(hours=hours)), iso(now), [], False
    for m in active_members(conn):
        recent = conn.execute(
            "SELECT 1 FROM xp_snapshots WHERE discord_id=? AND taken_at>? LIMIT 1", (m["discord_id"], fresh_after)
        ).fetchone()
        if recent:
            continue
        try:
            stats = await provider.fetch_player(m["rsn"])
        except PlayerNotFound:
            ok_any = True  # API worked; this RSN is just not on the hiscores
            continue
        except ProviderUnavailable as exc:
            if a := record_health(conn, API_HISCORES, False, str(exc)):
                alerts.append(a)
            return alerts
        ok_any = True
        store_snapshot(conn, m["discord_id"], taken_at, stats)
        milestones.evaluate_member(conn, m["discord_id"], {k: v for k, v in stats.items() if k != "Overall"})
    if a := record_health(conn, API_HISCORES, True, f"snapshot pass {taken_at}") if ok_any else None:
        alerts.append(a)
    set_setting(conn, "xp_snapshot_last_run", taken_at)
    return alerts


# ---- derived XP statistics -------------------------------------------


def _overall(conn: sqlite3.Connection, discord_id: int, where: str, params: tuple, order: str):
    """One Overall snapshot row picked by an SQL fragment (helper for gain calculations)."""
    return conn.execute(
        "SELECT taken_at, xp FROM xp_snapshots WHERE discord_id=? AND skill='Overall' "
        f"{where} ORDER BY taken_at {order} LIMIT 1",
        (discord_id, *params),
    ).fetchone()


def xp_gain(conn: sqlite3.Connection, discord_id: int, days: int, today: date | None = None) -> tuple[int, str] | None:
    """XP gained over the last ``days`` UTC days (today included) as ``(gain, baseline_time)``.

    The result is the latest snapshot minus the latest snapshot taken before the period boundary
    (00:00 UTC of the first day of the period). ``days=1`` therefore means "today so far", 7 means
    this and the previous 6 UTC days. With less history the earliest snapshot is the baseline, and
    ``baseline_time`` tells the reader how far back the number really reaches. None = no snapshots.
    """
    latest = _overall(conn, discord_id, "", (), "DESC")
    if latest is None:
        return None
    boundary = f"{((today or utcnow().date()) - timedelta(days=days - 1)).isoformat()}T00:00:00Z"
    base = _overall(conn, discord_id, "AND taken_at<?", (boundary,), "DESC") or _overall(
        conn, discord_id, "", (), "ASC"
    )
    return latest["xp"] - base["xp"], base["taken_at"]


def top_earners(conn: sqlite3.Connection, days: int, limit: int = 10, today: date | None = None) -> list[dict]:
    """Active members ranked by XP gained over ``days`` (top ``limit``, zero-gain members omitted)."""
    rows = []
    for m in active_members(conn):
        g = xp_gain(conn, m["discord_id"], days, today)
        if g and g[0] > 0:
            rows.append({"rsn": m["rsn"], "discord_id": m["discord_id"], "gain": g[0], "since": g[1]})
    return sorted(rows, key=lambda r: -r["gain"])[:limit]


def total_gain(conn: sqlite3.Connection, days: int, today: date | None = None) -> int:
    """Combined XP gained by all active members over ``days``."""
    return sum(g[0] for m in active_members(conn) if (g := xp_gain(conn, m["discord_id"], days, today)))


def latest_total(conn: sqlite3.Connection, discord_id: int) -> int | None:
    """The member's most recent Overall XP snapshot, or None if never snapshotted."""
    row = _overall(conn, discord_id, "", (), "DESC")
    return row["xp"] if row else None


# ---- Citadel adventure-log tracking ----------------------------------

_DATE_FORMATS = ("%d-%b-%Y %H:%M", "%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


def parse_activity_date(raw: str) -> datetime | None:
    """Parse a RuneMetrics date string as UTC; None if the format is not recognised."""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip().rstrip("Z"), fmt).replace(tzinfo=utcnow().tzinfo)
        except ValueError:
            continue
    return None


def apply_profile(conn: sqlite3.Connection, member: sqlite3.Row, profile) -> list[str]:
    """Apply new Citadel visit/cap log lines for one member; returns award descriptions.

    Each (date, text) is processed once. Lines older than the open cycle are recorded but not
    applied. On a member's very first poll, lines whose date cannot be parsed are baselined
    (recorded, not applied) so old history can never be paid by accident.
    """
    did = member["discord_id"]
    conn.execute(
        "UPDATE members SET profile_private=?, profile_checked_at=? WHERE discord_id=?",
        (int(profile.private), now_iso(), did),
    )
    if profile.private:
        return []
    first_poll = conn.execute("SELECT 1 FROM runemetrics_seen WHERE discord_id=? LIMIT 1", (did,)).fetchone() is None
    cycle_start = parse_utc(
        conn.execute("SELECT started_at FROM citadel_cycles WHERE id=?", (citadel.current_cycle(conn),)).fetchone()[
            "started_at"
        ]
    )
    fresh = []
    for act in profile.activities:
        kind = classify_activity(act.text)
        if kind is None:
            continue
        # The primary key is the once-only guard (dedupe by member + date + text).
        if conn.execute(
            "INSERT OR IGNORE INTO runemetrics_seen(discord_id,activity_date,text) VALUES(?,?,?)",
            (did, act.date, act.text),
        ).rowcount:
            fresh.append((act, kind))
    done: list[str] = []
    for act, kind in sorted(
        fresh, key=lambda p: parse_activity_date(p[0].date) or datetime.min.replace(tzinfo=utcnow().tzinfo)
    ):
        when = parse_activity_date(act.date)
        current = when >= cycle_start if when else not first_poll
        if not current:
            continue
        try:
            done += citadel.mark(conn, did, kind == "cap", 0, "rs3api")
        except MemberError:
            continue
        conn.execute(
            "UPDATE runemetrics_seen SET applied=1 WHERE discord_id=? AND activity_date=? AND text=?",
            (did, act.date, act.text),
        )
    return done


async def sync_citadel(conn: sqlite3.Connection, provider: XpProvider) -> list[str]:
    """Poll every active member's adventure log (only the latest 20 lines exist, so poll often)."""
    alerts, ok_any = [], False
    for m in active_members(conn):
        try:
            profile = await provider.fetch_profile(m["rsn"])
        except PlayerNotFound:
            ok_any = True
            continue
        except ProviderUnavailable as exc:
            if a := record_health(conn, API_RUNEMETRICS, False, str(exc)):
                alerts.append(a)
            return alerts
        ok_any = True
        apply_profile(conn, m, profile)
    if ok_any and (a := record_health(conn, API_RUNEMETRICS, True, "adventure logs polled")):
        alerts.append(a)
    return alerts


# ---- XP competitions --------------------------------------------------


def sync_competitions(conn: sqlite3.Connection) -> None:
    """Copy each entrant's contest-skill XP from the member snapshots (no API calls of its own).

    Running contests ('open') keep the latest snapshot per UTC day. When a contest enters review
    the latest snapshot becomes its final one and is recorded, so later passes do not move results.
    """
    comps = conn.execute(
        "SELECT * FROM competitions WHERE kind='xp' "
        "AND (status='open' OR (status='review' AND final_snapshot_at IS NULL))"
    ).fetchall()
    for comp in comps:
        start = comp["started_at"] or now_iso()
        for e in conn.execute("SELECT discord_id FROM competition_entries WHERE competition_id=?", (comp["id"],)):
            did = e["discord_id"]
            # Baseline: the last snapshot at or before the contest opened (up to one interval earlier than the
            # true start, so results may include a few hours of earlier XP). Stored under a key that sorts first.
            base = conn.execute(
                "SELECT xp FROM xp_snapshots WHERE discord_id=? AND skill=? AND taken_at<=? "
                "ORDER BY taken_at DESC LIMIT 1",
                (did, comp["skill"], start),
            ).fetchone()
            if base:
                conn.execute(
                    "INSERT OR REPLACE INTO competition_snapshots(competition_id,discord_id,day,xp) "
                    "VALUES(?,?,'0start',?)",
                    (comp["id"], did, base["xp"]),
                )
            later = conn.execute(
                "SELECT taken_at, xp FROM xp_snapshots WHERE discord_id=? AND skill=? AND taken_at>? ORDER BY taken_at",
                (did, comp["skill"], start),
            ).fetchall()
            for r in later:  # ascending, so the latest snapshot of each UTC day wins
                conn.execute(
                    "INSERT OR REPLACE INTO competition_snapshots(competition_id,discord_id,day,xp) VALUES(?,?,?,?)",
                    (comp["id"], did, r["taken_at"][:10], r["xp"]),
                )
        if comp["status"] == "review":
            conn.execute("UPDATE competitions SET final_snapshot_at=? WHERE id=?", (now_iso(), comp["id"]))


def competition_standings(conn: sqlite3.Connection, cid: int) -> list[dict]:
    """XP gained per entrant (latest minus first snapshot), highest first; ties are left for the admin."""
    out = []
    for e in conn.execute("SELECT discord_id, label FROM competition_entries WHERE competition_id=?", (cid,)):
        snaps = conn.execute(
            "SELECT xp FROM competition_snapshots WHERE competition_id=? AND discord_id=? ORDER BY day",
            (cid, e["discord_id"]),
        ).fetchall()
        if snaps:
            out.append({"discord_id": e["discord_id"], "label": e["label"], "gain": snaps[-1]["xp"] - snaps[0]["xp"]})
    return sorted(out, key=lambda r: -r["gain"])
