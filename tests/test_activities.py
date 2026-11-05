"""Surveys, giveaways, competitions and events (domain rules)."""

import random
from datetime import timedelta

import pytest

from dark_asylum import competitions, events, giveaways, ledger, members, surveys
from dark_asylum.db import utcnow


def _member(conn, did, rsn, spending=0):
    """Create an active member, optionally with spendable points."""
    members.link_rsn(conn, did, rsn, did)
    conn.execute("UPDATE settings SET value='0' WHERE key='ranking_target_points'")
    if spending:
        ledger.award_points(conn, did, spending, "seed", 9)


# ---- surveys ----------------------------------------------------------


def test_survey_vote_change_and_close(conn):
    _member(conn, 1, "Alpha")
    sid = surveys.create_survey(conn, "Pick", ["A", "B"], False, 9)
    a, b = [r["id"] for r in surveys.results(conn, sid)]
    surveys.vote(conn, sid, a, 1, "salt")
    surveys.vote(conn, sid, b, 1, "salt")  # changing answer replaces the vote
    assert [r["votes"] for r in surveys.results(conn, sid)] == [0, 1]
    surveys.close_survey(conn, sid, 9)
    with pytest.raises(surveys.SurveyError):
        surveys.vote(conn, sid, a, 1, "salt")


def test_anonymous_survey_stores_no_identity(conn):
    _member(conn, 12345, "Alpha")
    sid = surveys.create_survey(conn, "Secret?", ["Yes", "No"], True, 9)
    opt = surveys.results(conn, sid)[0]["id"]
    surveys.vote(conn, sid, opt, 12345, "salt")
    refs = [r["voter_ref"] for r in conn.execute("SELECT voter_ref FROM survey_votes")]
    assert refs and "12345" not in refs[0]


def test_survey_members_only_and_validation(conn):
    sid = surveys.create_survey(conn, "Q", ["A", "B"], False, 9)
    with pytest.raises(surveys.SurveyError):
        surveys.vote(conn, sid, surveys.results(conn, sid)[0]["id"], 99, "s")  # guest
    with pytest.raises(surveys.SurveyError):
        surveys.create_survey(conn, "Q", ["only"], False, 9)


# ---- giveaways --------------------------------------------------------


def test_giveaway_needs_bank_funds(conn):
    with pytest.raises(giveaways.GiveawayError):
        giveaways.create_giveaway(conn, "Prize", 1_000_000, 10, "", 9)
    ledger.record_bank(conn, 5_000_000, "seed", 9)
    assert giveaways.create_giveaway(conn, "Prize", 1_000_000, 10, "", 9)
    with pytest.raises(giveaways.GiveawayError):
        giveaways.create_giveaway(conn, "Huge", ledger.MAX_GP_REWARD + 1, 10, "", 9)


def test_entries_free_first_cap_five_and_points_deducted(conn):
    ledger.record_bank(conn, 5_000_000, "seed", 9)
    _member(conn, 1, "Alpha", spending=100)
    gid = giveaways.create_giveaway(conn, "P", 1_000_000, 10, "", 9)
    assert "free" in giveaways.enter(conn, gid, 1)
    for _ in range(4):
        giveaways.enter(conn, gid, 1)
    with pytest.raises(giveaways.GiveawayError):
        giveaways.enter(conn, gid, 1)  # sixth entry refused
    assert conn.execute("SELECT spending_points FROM members").fetchone()[0] == 60  # 4 paid x 10


def test_entry_rejected_without_points_and_guests(conn):
    ledger.record_bank(conn, 5_000_000, "seed", 9)
    _member(conn, 1, "Alpha", spending=5)
    gid = giveaways.create_giveaway(conn, "P", 1_000_000, 10, "", 9)
    giveaways.enter(conn, gid, 1)
    with pytest.raises(ledger.LedgerError):
        giveaways.enter(conn, gid, 1)  # paid entry costs 10, has 5
    with pytest.raises(members.MemberError):
        giveaways.enter(conn, gid, 77)  # guest


def test_cancel_refunds_and_draw_payout(conn):
    ledger.record_bank(conn, 5_000_000, "seed", 9)
    _member(conn, 1, "Alpha", spending=50)
    g1 = giveaways.create_giveaway(conn, "A", 1_000_000, 10, "", 9)
    giveaways.enter(conn, g1, 1)
    giveaways.enter(conn, g1, 1)
    assert giveaways.cancel(conn, g1, 9) == 10
    assert conn.execute("SELECT spending_points FROM members").fetchone()[0] == 50
    g2 = giveaways.create_giveaway(conn, "B", 1_000_000, 10, "", 9)
    giveaways.enter(conn, g2, 1)
    assert giveaways.draw(conn, g2, 9, random.Random(1)) == 1
    giveaways.record_payout(conn, g2, 9)
    assert ledger.bank_balance(conn) == 4_000_000
    with pytest.raises(giveaways.GiveawayError):
        giveaways.record_payout(conn, g2, 9)  # no double payout


def test_payout_never_overdraws(conn):
    ledger.record_bank(conn, 1_000_000, "seed", 9)
    _member(conn, 1, "Alpha")
    gid = giveaways.create_giveaway(conn, "P", 1_000_000, 0, "", 9)
    giveaways.enter(conn, gid, 1)
    giveaways.draw(conn, gid, 9)
    ledger.record_bank(conn, -500_000, "spent elsewhere", 9)
    with pytest.raises(ledger.LedgerError):
        giveaways.record_payout(conn, gid, 9)


# ---- competitions -----------------------------------------------------


def _open_comp(conn, kind="proof", rewarded=True):
    """Create and open a competition."""
    cid = competitions.create(conn, "Bingo", kind, "Mining" if kind == "xp" else "", rewarded, 9)
    competitions.set_status(conn, cid, "open", 9)
    return cid


def test_lifecycle_rejects_illegal_jump(conn):
    cid = competitions.create(conn, "X", "proof", "", False, 9)
    with pytest.raises(competitions.CompetitionError):
        competitions.set_status(conn, cid, "finished", 9)


def test_prize_contest_members_only_fun_allows_guests(conn):
    prize = _open_comp(conn)
    fun = _open_comp(conn, rewarded=False)
    with pytest.raises(competitions.CompetitionError):
        competitions.join(conn, prize, 50, "guest")
    competitions.join(conn, fun, 50, "guest")


def test_proof_approval_and_awards_paid_once(conn):
    for i, n in ((1, "A"), (2, "B"), (3, "C")):
        _member(conn, i, n)
    cid = _open_comp(conn)
    for i in (1, 2, 3):
        competitions.join(conn, cid, i, f"m{i}")
    competitions.submit_proof(conn, cid, 1, "link")
    competitions.submit_proof(conn, cid, 2, "link")
    entries = {r["discord_id"]: r["id"] for r in conn.execute("SELECT id, discord_id FROM competition_entries")}
    competitions.review_proof(conn, entries[1], True, 9)
    competitions.review_proof(conn, entries[2], False, 9)
    competitions.set_status(conn, cid, "review", 9)
    competitions.finish(conn, cid, {1: entries[1]}, 9)

    def total(i):
        """A member's combined ranking + spending points."""
        return conn.execute("SELECT spending_points+ranking_points FROM members WHERE discord_id=?", (i,)).fetchone()[0]

    assert total(1) == 100 + 200  # participation + 1st
    assert total(2) == 0 and total(3) == 0  # rejected / no proof
    with pytest.raises(competitions.CompetitionError):
        competitions.finish(conn, cid, {1: entries[1]}, 9)  # no second payout


# ---- events -----------------------------------------------------------


def test_reminders_fire_once_and_respect_switches(conn):
    soon = (utcnow() + timedelta(hours=10)).strftime("%Y-%m-%d %H:%M")
    eid = events.create_event(conn, "Raid", soon, 0, True, True, 9)
    due = events.due_reminders(conn)
    assert [d[2] for d in due] == ["24h"]  # within 24h, outside the 1h window
    events.mark_reminder_sent(conn, eid, due[0][1], "24h")
    assert events.due_reminders(conn) == []
    later = utcnow() + timedelta(hours=9, minutes=30)
    assert [d[2] for d in events.due_reminders(conn, later)] == ["1h"]


def test_recurrence_and_cancel(conn):
    past = (utcnow() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M")
    eid = events.create_event(conn, "Weekly", past, 7, True, True, 9)
    ev = conn.execute("SELECT * FROM events").fetchone()
    nxt = events.next_occurrence(ev, utcnow())
    assert nxt > utcnow() and nxt - utcnow() <= timedelta(days=7)
    events.cancel_event(conn, eid, 9)
    assert events.due_reminders(conn) == []


def test_attendance_pays_once_and_local_time(conn):
    _member(conn, 1, "Alpha")
    eid = events.create_event(conn, "E", "2030-01-01 12:00", 0, True, True, 9)
    assert events.confirm_attendance(conn, eid, "2030-01-01T12:00:00Z", 1, 9) == 150
    with pytest.raises(events.EventError):
        events.confirm_attendance(conn, eid, "2030-01-01T12:00:00Z", 1, 9)
    assert "13:00" in events.local_time("2030-01-01T12:00:00Z", "Europe/Paris")
    assert "12:00" in events.local_time("2030-01-01T12:00:00Z", "Not/AZone")
