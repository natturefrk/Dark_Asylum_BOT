"""RS3 data layer: parsers, HTTP client, snapshot sync, Citadel polling, gains and health (all mocked)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from dark_asylum import citadel, competitions, members, rs3api, rs3sync
from dark_asylum.db import get_setting, iso, set_setting
from dark_asylum.xp import Activity, ClanMate, PlayerNotFound, Profile, ProviderUnavailable, XpProvider


def run(coro):
    """Run one coroutine to completion (keeps the tests free of an async plugin)."""
    return asyncio.run(coro)


class FakeProvider(XpProvider):
    """In-memory provider: canned hiscores, roster and profiles, or a simulated outage."""

    def __init__(self, stats=None, roster=None, profiles=None):
        """Store canned data keyed by RSN; ``down`` switches every call to an outage."""
        self.stats, self.roster, self.profiles = stats or {}, roster or [], profiles or {}
        self.down, self.calls = False, []

    async def fetch_skills(self, rsn):
        """Skills without Overall (unused by the sync, required by the interface)."""
        return {k: v for k, v in (await self.fetch_player(rsn)).items() if k != "Overall"}

    async def fetch_player(self, rsn):
        """Canned hiscores for ``rsn`` (records the call so tests can count requests)."""
        self.calls.append(rsn)
        if self.down:
            raise ProviderUnavailable("simulated outage")
        if rsn not in self.stats:
            raise PlayerNotFound(rsn)
        return dict(self.stats[rsn])

    async def fetch_roster(self, clan_name):
        """Canned roster."""
        if self.down:
            raise ProviderUnavailable("simulated outage")
        return self.roster

    async def fetch_profile(self, rsn):
        """Canned profile."""
        if self.down:
            raise ProviderUnavailable("simulated outage")
        return self.profiles[rsn]


def _member(conn, did, rsn):
    """An active member with ranking threshold disabled."""
    members.link_rsn(conn, did, rsn, did)
    conn.execute("UPDATE settings SET value='0' WHERE key='ranking_target_points'")


def _stats(total, mining=0):
    """Hiscore dict with an Overall total and a Mining value."""
    return {"Overall": total, "Mining": mining}


T0 = datetime(2030, 3, 10, 12, 0, tzinfo=UTC)


# ---- parsers ------------------------------------------------------------


def test_roster_parser_handles_nbsp_and_bad_rows():
    body = (
        b"Clanmate, Clan Rank, Total XP, Kills\nSome\xa0Name, Owner, 1500, 3\n"
        b"broken,Rank,notanumber,1\n,,,\nOk, Recruit, 7, 0\n"
    )
    mates = rs3api.parse_clan_roster(body)
    assert [m.rsn for m in mates] == ["Some Name", "Ok"]  # nbsp normalised, bad rows skipped
    assert mates[0].total_xp == 1500


def test_roster_parser_rejects_wrong_format():
    with pytest.raises(ProviderUnavailable):
        rs3api.parse_clan_roster(b"<html>maintenance</html>")


def test_hiscores_parser_skips_unranked_and_requires_overall():
    body = json.dumps({"skills": [{"name": "Overall", "xp": 100}, {"name": "Mining", "xp": -1}]}).encode()
    assert rs3api.parse_hiscores(body) == {"Overall": 100}
    for bad in (b"not json", json.dumps({"skills": [{"name": "Mining", "xp": 5}]}).encode(), b'{"x":1}'):
        with pytest.raises(ProviderUnavailable):
            rs3api.parse_hiscores(bad)


def test_profile_parser_variants():
    ok = json.dumps({"activities": [{"date": "01-Jan-2030 10:00", "text": "Visited my Clan Citadel.", "details": ""}]})
    assert rs3api.parse_profile(ok.encode()).activities[0].text.startswith("Visited")
    assert rs3api.parse_profile(b'{"error":"PROFILE_PRIVATE"}').private is True
    with pytest.raises(PlayerNotFound):
        rs3api.parse_profile(b'{"error":"NO_PROFILE"}')
    for bad in (b"garbage", b"[1]", b'{"error":"WEIRD"}', b'{"activities":"x"}'):
        with pytest.raises(ProviderUnavailable):
            rs3api.parse_profile(bad)


def test_classify_activity_is_case_insensitive():
    assert rs3api.classify_activity("CAPPED at my Clan Citadel.") == "cap"
    assert rs3api.classify_activity("Visited my clan citadel.") == "visit"
    assert rs3api.classify_activity("Levelled up Mining") is None


# ---- HTTP client ----------------------------------------------------------


class ScriptedFetcher(rs3api.HttpFetcher):
    """HttpFetcher whose network call returns scripted responses and whose sleeps are recorded."""

    def __init__(self, script):
        """``script`` is a list of (status, body, retry_after) or exceptions."""
        super().__init__(min_interval=0.0, retries=3)
        self.script, self.slept = list(script), []

    async def _once(self, url, params):
        """Pop the next scripted response."""
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def _sleep(self, seconds):
        """Record instead of sleeping."""
        self.slept.append(seconds)


def test_fetcher_retries_with_backoff_then_succeeds():
    f = ScriptedFetcher([(500, b"", None), (429, b"", "7"), (200, b"ok", None)])
    assert run(f.get("u", {})) == (200, b"ok")
    assert f.slept == [2.0, 7.0]  # exponential backoff, then the server's Retry-After hint


def test_fetcher_returns_404_and_fails_after_retries():
    assert run(ScriptedFetcher([(404, b"", None)]).get("u", {})) == (404, b"")
    with pytest.raises(ProviderUnavailable):
        run(ScriptedFetcher([TimeoutError(), (503, b"", None), (500, b"", None)]).get("u", {}))


def test_provider_maps_404_to_not_found():
    prov = rs3api.Rs3Provider(ScriptedFetcher([(404, b"", None)]))
    with pytest.raises(PlayerNotFound):
        run(prov.fetch_player("Nobody"))


# ---- health alerts ----------------------------------------------------------


def test_health_alerts_only_on_state_change(conn):
    assert rs3sync.record_health(conn, "rs3_hiscores", True) is None
    assert "DOWN" in rs3sync.record_health(conn, "rs3_hiscores", False, "boom")
    assert rs3sync.record_health(conn, "rs3_hiscores", False, "boom") is None
    assert "recovered" in rs3sync.record_health(conn, "rs3_hiscores", True)


# ---- roster -----------------------------------------------------------------


def test_roster_sync_and_lookup(conn):
    set_setting(conn, "clan_name", "Dark Asylum")
    prov = FakeProvider(roster=[ClanMate("Some Name", "Owner", 5000, 1)])
    run(rs3sync.sync_roster(conn, prov))
    assert rs3sync.clan_xp_for(conn, "some_name")["total_xp"] == 5000  # underscore/case insensitive
    prov.down = True
    alerts = run(rs3sync.sync_roster(conn, prov))
    assert alerts and "DOWN" in alerts[0]
    assert rs3sync.clan_xp_for(conn, "Some Name") is not None  # stale data kept during an outage


# ---- XP snapshots -------------------------------------------------------------


def test_snapshot_interval_default_and_clamp(conn):
    assert rs3sync.snapshot_hours(conn) == 6
    set_setting(conn, "xp_snapshot_hours", "99")
    assert rs3sync.snapshot_hours(conn) == 24
    set_setting(conn, "xp_snapshot_hours", "junk")
    assert rs3sync.snapshot_hours(conn) == 6


def test_snapshot_pass_respects_interval_and_persists_last_run(conn):
    _member(conn, 1, "Alpha")
    prov = FakeProvider({"Alpha": _stats(1000)})
    run(rs3sync.sync_xp(conn, prov, T0))
    assert get_setting(conn, "xp_snapshot_last_run") == iso(T0)
    run(rs3sync.sync_xp(conn, prov, T0 + timedelta(hours=5, minutes=59)))  # too early: a restart changes nothing
    assert prov.calls == ["Alpha"]
    run(rs3sync.sync_xp(conn, prov, T0 + timedelta(hours=6)))
    assert prov.calls == ["Alpha", "Alpha"]
    assert conn.execute("SELECT COUNT(DISTINCT taken_at) FROM xp_snapshots").fetchone()[0] == 2


def test_snapshot_interval_is_configurable(conn):
    _member(conn, 1, "Alpha")
    set_setting(conn, "xp_snapshot_hours", "1")
    prov = FakeProvider({"Alpha": _stats(1)})
    run(rs3sync.sync_xp(conn, prov, T0))
    run(rs3sync.sync_xp(conn, prov, T0 + timedelta(hours=1)))
    assert len(prov.calls) == 2


def test_outage_aborts_pass_and_is_retried(conn):
    _member(conn, 1, "Alpha")
    prov = FakeProvider({"Alpha": _stats(1)})
    prov.down = True
    alerts = run(rs3sync.sync_xp(conn, prov, T0))
    assert alerts and get_setting(conn, "xp_snapshot_last_run") == ""  # not marked complete
    prov.down = False
    run(rs3sync.sync_xp(conn, prov, T0 + timedelta(minutes=15)))
    assert conn.execute("SELECT COUNT(*) FROM xp_snapshots").fetchone()[0] == 2


def test_unlisted_player_is_skipped(conn):
    _member(conn, 1, "Ghost")
    run(rs3sync.sync_xp(conn, FakeProvider(), T0))
    assert conn.execute("SELECT COUNT(*) FROM xp_snapshots").fetchone()[0] == 0


def test_milestone_awarded_once_from_snapshots(conn):
    _member(conn, 1, "Alpha")
    prov = FakeProvider({"Alpha": _stats(20_000_000, mining=14_000_000)})
    run(rs3sync.sync_xp(conn, prov, T0))
    run(rs3sync.sync_xp(conn, prov, T0 + timedelta(hours=7)))
    assert members.get_member(conn, 1)["spending_points"] == 250  # paid exactly once


def test_overall_never_triggers_skill_milestones(conn):
    _member(conn, 1, "Alpha")
    run(rs3sync.sync_xp(conn, FakeProvider({"Alpha": _stats(500_000_000, mining=1)}), T0))
    assert members.get_member(conn, 1)["spending_points"] == 0


# ---- gains --------------------------------------------------------------------


def _snap(conn, did, when, total):
    """Insert one Overall snapshot."""
    rs3sync.store_snapshot(conn, did, iso(when), {"Overall": total})


def test_gains_use_latest_snapshot_before_period_boundary(conn):
    _member(conn, 1, "Alpha")
    today = date(2030, 3, 10)
    _snap(conn, 1, datetime(2030, 3, 2, 23, 0, tzinfo=UTC), 1000)  # before the 7-day boundary (Mar 4)
    _snap(conn, 1, datetime(2030, 3, 3, 23, 0, tzinfo=UTC), 1500)  # latest before it: the baseline
    _snap(conn, 1, datetime(2030, 3, 9, 23, 0, tzinfo=UTC), 2500)  # last snapshot of yesterday
    _snap(conn, 1, datetime(2030, 3, 10, 6, 0, tzinfo=UTC), 2600)
    _snap(conn, 1, datetime(2030, 3, 10, 12, 0, tzinfo=UTC), 2900)
    assert rs3sync.xp_gain(conn, 1, 7, today)[0] == 2900 - 1500
    assert rs3sync.xp_gain(conn, 1, 1, today)[0] == 2900 - 2500  # today = latest minus last snapshot of yesterday


def test_gain_with_short_history_uses_earliest_snapshot(conn):
    _member(conn, 1, "Alpha")
    _snap(conn, 1, datetime(2030, 3, 10, 0, 0, tzinfo=UTC), 100)
    _snap(conn, 1, datetime(2030, 3, 10, 6, 0, tzinfo=UTC), 160)
    gain, since = rs3sync.xp_gain(conn, 1, 30, date(2030, 3, 10))
    assert gain == 60 and since.startswith("2030-03-10T00")
    assert rs3sync.xp_gain(conn, 99, 7) is None


def test_top_earners_sorted_limited_and_zero_excluded(conn):
    today = date(2030, 3, 10)
    for i in range(1, 13):
        _member(conn, i, f"Player{i}")
        _snap(conn, i, datetime(2030, 3, 9, 23, 0, tzinfo=UTC), 0)
        _snap(conn, i, datetime(2030, 3, 10, 5, 0, tzinfo=UTC), i * 10 if i != 3 else 0)
    top = rs3sync.top_earners(conn, 1, today=today)
    assert len(top) == 10 and top[0]["rsn"] == "Player12" and all(t["rsn"] != "Player3" for t in top)
    assert rs3sync.total_gain(conn, 1, today) == sum(i * 10 for i in range(1, 13) if i != 3)


# ---- Citadel adventure log --------------------------------------------------------


def _profile(*lines):
    """A readable profile with (date, text) log lines."""
    return Profile(False, [Activity(d, t, "") for d, t in lines])


def _apply(conn, did, profile):
    """Run apply_profile for a member id."""
    return rs3sync.apply_profile(conn, members.get_member(conn, did), profile)


def _stamp(conn, when="2000-01-01T00:00:00Z"):
    """Move the open cycle's start time (so test log dates can be 'inside' it)."""
    conn.execute("UPDATE citadel_cycles SET started_at=? WHERE ended_at IS NULL", (when,))


def test_visit_then_cap_awards_once_each(conn):
    _member(conn, 1, "Alpha")
    _stamp(conn)
    visit = ("01-Jan-2030 10:00", "Visited my Clan Citadel.")
    assert _apply(conn, 1, _profile(visit))
    assert members.get_member(conn, 1)["spending_points"] == 50
    assert _apply(conn, 1, _profile(visit)) == []  # same line again: deduped
    _apply(conn, 1, _profile(visit, ("02-Jan-2030 10:00", "Capped at my Clan Citadel.")))
    assert members.get_member(conn, 1)["spending_points"] == 150  # +100 cap bonus, visit not paid twice


def test_cap_alone_implies_visit(conn):
    _member(conn, 1, "Alpha")
    _stamp(conn)
    _apply(conn, 1, _profile(("01-Jan-2030 10:00", "Capped at my Clan Citadel.")))
    assert members.get_member(conn, 1)["spending_points"] == 150


def test_lines_from_before_the_cycle_are_ignored(conn):
    _member(conn, 1, "Alpha")
    _stamp(conn, "2030-06-01T00:00:00Z")
    assert _apply(conn, 1, _profile(("01-Jan-2030 10:00", "Visited my Clan Citadel."))) == []
    assert members.get_member(conn, 1)["spending_points"] == 0


def test_first_poll_baselines_unparseable_dates(conn):
    _member(conn, 1, "Alpha")
    assert _apply(conn, 1, _profile(("someday", "Visited my Clan Citadel."))) == []
    assert members.get_member(conn, 1)["spending_points"] == 0


def test_private_profile_is_flagged_and_clears(conn):
    _member(conn, 1, "Alpha")
    _apply(conn, 1, Profile(True, []))
    assert members.get_member(conn, 1)["profile_private"] == 1
    _apply(conn, 1, _profile())
    assert members.get_member(conn, 1)["profile_private"] == 0


def test_unrelated_log_lines_are_ignored(conn):
    _member(conn, 1, "Alpha")
    assert _apply(conn, 1, _profile(("01-Jan-2030 10:00", "Levelled up Mining."))) == []


def test_sync_citadel_handles_outage_and_missing_profile(conn):
    _member(conn, 1, "Alpha")
    _member(conn, 2, "Beta")
    prov = FakeProvider(profiles={"Alpha": Profile(True, [])})
    prov.profiles["Beta"] = Profile(True, [])
    assert run(rs3sync.sync_citadel(conn, prov)) == []
    prov.down = True
    assert "DOWN" in run(rs3sync.sync_citadel(conn, prov))[0]
    assert citadel.current_cycle(conn)


# ---- XP competitions ------------------------------------------------------------------


def test_contest_gain_from_start_baseline_to_final(conn):
    _member(conn, 1, "Alpha")
    cid = competitions.create(conn, "Mining week", "xp", "Mining", False, 9)
    competitions.set_status(conn, cid, "open", 9)
    competitions.join(conn, cid, 1, "Alpha")
    start = conn.execute("SELECT started_at FROM competitions WHERE id=?", (cid,)).fetchone()[0]
    begin = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    rs3sync.store_snapshot(conn, 1, iso(begin - timedelta(hours=1)), {"Mining": 1000})
    rs3sync.store_snapshot(conn, 1, iso(begin + timedelta(hours=7)), {"Mining": 1800})
    rs3sync.sync_competitions(conn)
    assert rs3sync.competition_standings(conn, cid)[0]["gain"] == 800
    competitions.set_status(conn, cid, "review", 9)
    rs3sync.store_snapshot(conn, 1, iso(begin + timedelta(hours=13)), {"Mining": 2500})
    rs3sync.sync_competitions(conn)
    assert rs3sync.competition_standings(conn, cid)[0]["gain"] == 1500
    rs3sync.store_snapshot(conn, 1, iso(begin + timedelta(hours=19)), {"Mining": 9999})
    rs3sync.sync_competitions(conn)  # finalised: later XP must not move the result
    assert rs3sync.competition_standings(conn, cid)[0]["gain"] == 1500
