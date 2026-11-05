"""Milestones, redemptions, bossing goals and purge jobs (domain rules)."""

from datetime import date

import pytest

from dark_asylum import bossing, ledger, members, milestones, purge, redemptions


def _member(conn, did, rsn, spending=0):
    """Create an active member, optionally with spendable points."""
    members.link_rsn(conn, did, rsn, did)
    conn.execute("UPDATE settings SET value='0' WHERE key='ranking_target_points'")
    if spending:
        ledger.award_points(conn, did, spending, "seed", 9)


def _points(conn, did):
    """Current spending balance."""
    return members.get_member(conn, did)["spending_points"]


# ---- milestones -------------------------------------------------------


def test_skill_milestones_pay_once(conn):
    _member(conn, 1, "Alpha")
    first = milestones.evaluate_member(conn, 1, {"Mining": 14_000_000})
    assert len(first) == 1  # only the level-99 threshold is reached
    assert milestones.evaluate_member(conn, 1, {"Mining": 14_000_000}) == []
    assert _points(conn, 1) == 250


def test_clan_milestone_pays_each_member_once(conn):
    _member(conn, 1, "Alpha")
    _member(conn, 2, "Beta")
    milestones.add_milestone(conn, "clan", "", 1_000_000, 40, 9)
    assert len(milestones.evaluate_clan(conn, 2_000_000)) == 1
    assert milestones.evaluate_clan(conn, 3_000_000) == []
    assert _points(conn, 1) == 40 and _points(conn, 2) == 40


def test_milestone_validation_and_duplicates(conn):
    with pytest.raises(milestones.MilestoneError):
        milestones.add_milestone(conn, "bogus", "", 1, 1, 9)
    milestones.add_milestone(conn, "skill", "Mining", 5, 1, 9)
    with pytest.raises(milestones.MilestoneError):
        milestones.add_milestone(conn, "skill", "Mining", 5, 1, 9)


# ---- redemptions ------------------------------------------------------


def test_redemption_flow_and_monthly_limit(conn):
    _member(conn, 1, "Alpha", spending=2000)
    ledger.record_bank(conn, 500_000_000, "seed", 9)
    w1 = redemptions.add_window(conn, "2030-01-15", "mid", 9)
    w2 = redemptions.add_window(conn, "2030-01-31", "end", 9)
    today = date(2030, 1, 1)
    r1 = redemptions.request(conn, 1, w1, "gp", 10_000_000, "", today)
    r2 = redemptions.request(conn, 1, w2, "gp", 10_000_000, "", today)
    redemptions.approve(conn, r1, 9)
    assert _points(conn, 1) == 2000 - 100
    with pytest.raises(redemptions.RedemptionError):
        redemptions.approve(conn, r2, 9)  # same month slot already taken
    redemptions.pay(conn, r1, 9)
    assert ledger.bank_balance(conn) == 490_000_000


def test_redemption_limits_and_bank_floor(conn):
    _member(conn, 1, "Alpha", spending=5000)
    w = redemptions.add_window(conn, "2030-02-15", "mid", 9)
    today = date(2030, 2, 1)
    with pytest.raises(redemptions.RedemptionError):
        redemptions.request(conn, 1, w, "gp", 150_000_001, "", today)
    r = redemptions.request(conn, 1, w, "gp", 1_000_000, "", today)
    redemptions.approve(conn, r, 9)
    with pytest.raises(ledger.LedgerError):
        redemptions.pay(conn, r, 9)  # empty bank
    redemptions.cancel_approved(conn, r, 9)
    assert _points(conn, 1) == 5000


# ---- bossing goals ----------------------------------------------------


def test_goal_lifecycle(conn):
    _member(conn, 1, "Alpha")
    ledger.record_bank(conn, 1_000_000, "seed", 9)
    bossing.save_item(conn, "Sword", 1000, "manual", True, 9)
    item = conn.execute("SELECT id FROM bossing_items").fetchone()["id"]
    gid = bossing.start_goal(conn, 1, item, 9)  # default 10% off locks the target at 900
    with pytest.raises(bossing.GoalError):
        bossing.start_goal(conn, 1, item, 9)  # one open goal per member
    with pytest.raises(bossing.GoalError):
        bossing.contribute(conn, gid, 901, 9)
    assert bossing.contribute(conn, gid, 900, 9) == 0
    assert bossing.open_goal(conn, 1)["status"] == "ready"
    bossing.fulfill(conn, gid, 9)
    assert ledger.bank_balance(conn) == 1_000_000 + 900 - 1000


def test_goal_cancel_refunds_and_discount_cap(conn):
    _member(conn, 1, "Alpha")
    bossing.save_item(conn, "Bow", 1000, "manual", True, 9)
    item = conn.execute("SELECT id FROM bossing_items").fetchone()["id"]
    with pytest.raises(bossing.GoalError):
        bossing.set_member_discount(conn, 1, 51, 9)
    bossing.set_member_discount(conn, 1, 50, 9)
    gid = bossing.start_goal(conn, 1, item, 9)
    bossing.contribute(conn, gid, 100, 9)
    assert bossing.cancel(conn, gid, 9) == 100
    assert ledger.bank_balance(conn) == 0


def test_unconfirmed_price_blocks_goal(conn):
    _member(conn, 1, "Alpha")
    bossing.save_item(conn, "Axe", 1000, "ge_reference", False, 9)
    item = conn.execute("SELECT id FROM bossing_items").fetchone()["id"]
    with pytest.raises(bossing.GoalError):
        bossing.start_goal(conn, 1, item, 9)


# ---- purge ------------------------------------------------------------


def test_purge_queue_validation_and_audit(conn):
    with pytest.raises(purge.PurgeError):
        purge.queue(conn, 5, 45, 9)
    jid = purge.queue(conn, 5, 30, 9)
    purge.finish(conn, jid, 12, "ok")
    assert conn.execute("SELECT status,deleted FROM purge_jobs").fetchone()["deleted"] == 12


def test_reminder_lists_member_timezones(conn):
    from dark_asylum import events

    _member(conn, 1, "Alpha")
    conn.execute("UPDATE members SET timezone='America/New_York' WHERE discord_id=1")
    text = events.reminder_text(conn, "Raid", "2030-01-01T12:00:00Z", "1h")
    assert "America/New_York: Tue 01 Jan 07:00" in text and "12:00 UTC" in text
