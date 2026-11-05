"""Citadel cycle points, idempotence and archiving."""

import pytest

from dark_asylum import citadel, ledger, members


def _total(conn, did):
    """Total points ever awarded to a member."""
    return conn.execute(
        "SELECT COALESCE(SUM(ranking_delta+spending_delta),0) FROM points_ledger WHERE discord_id=? AND kind='award'",
        (did,),
    ).fetchone()[0]


def test_visit_cap_and_no_double_pay(conn):
    members.link_rsn(conn, 1, "Alpha", 1)
    citadel.mark(conn, 1, False, 9)
    assert _total(conn, 1) == 50
    citadel.mark(conn, 1, False, 9)  # repeat visit pays nothing
    assert _total(conn, 1) == 50
    citadel.mark(conn, 1, True, 9)  # cap adds only the bonus -> 150 total
    assert _total(conn, 1) == 150
    citadel.mark(conn, 1, True, 9)
    assert _total(conn, 1) == 150


def test_cap_implies_visit(conn):
    members.link_rsn(conn, 1, "Alpha", 1)
    citadel.mark(conn, 1, True, 9)
    assert _total(conn, 1) == 150 and citadel.status(conn, 1)["visited"] == 1


def test_reset_archives_and_reopens(conn):
    members.link_rsn(conn, 1, "Alpha", 1)
    members.link_rsn(conn, 2, "Beta", 2)
    citadel.mark(conn, 1, True, 9)
    citadel.mark(conn, 2, False, 9)
    old = citadel.current_cycle(conn)
    new = citadel.reset_cycle(conn, 9)
    assert new != old and citadel.current_cycle(conn) == new
    row = conn.execute("SELECT * FROM citadel_cycles WHERE id=?", (old,)).fetchone()
    assert (row["visitors"], row["cappers"]) == (2, 1) and row["ended_at"]
    citadel.mark(conn, 1, False, 9)  # new cycle pays again
    assert _total(conn, 1) == 200


def test_inactive_member_rejected(conn):
    members.link_rsn(conn, 1, "Alpha", 1)
    members.remove_member(conn, 1, 9, "left", "test")
    with pytest.raises(members.MemberError):
        citadel.mark(conn, 1, False, 9)
    assert ledger.points_history  # ledger helpers remain importable
