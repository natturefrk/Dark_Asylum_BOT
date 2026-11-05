"""Domain rules: bank, points, members, strikes, durations, backups."""

import pytest

from dark_asylum import backup, ledger, members, rules
from dark_asylum.db import purge_old_audit

ID1, ID2 = 1001, 1002


def test_bank_never_negative(conn):
    ledger.record_bank(conn, 1000, "deposit", 1)
    with pytest.raises(ledger.LedgerError):
        ledger.record_bank(conn, -1001, "too much", 1)
    assert ledger.record_bank(conn, -1000, "ok", 1) == 0


def test_bank_requires_reason(conn):
    with pytest.raises(ledger.LedgerError):
        ledger.record_bank(conn, 5, "  ", 1)


def test_duplicate_rsn_rejected_and_replaced(conn):
    members.link_rsn(conn, ID1, "Dark One", ID1)
    with pytest.raises(members.MemberError):
        members.link_rsn(conn, ID2, "dark one", ID2)  # case-insensitive
    members.link_rsn(conn, ID1, "Renamed", ID1)  # replaces, no alias kept
    members.link_rsn(conn, ID2, "Dark One", ID2)  # now free


def test_points_fill_ranking_first_then_spending(conn):
    members.link_rsn(conn, ID1, "Alpha", ID1)
    conn.execute("UPDATE settings SET value='120' WHERE key='ranking_target_points'")
    assert ledger.award_points(conn, ID1, 100, "event", 9) == (100, 0)
    assert ledger.award_points(conn, ID1, 100, "event", 9) == (20, 80)
    conn.execute("UPDATE members SET ranking_opt_out=1 WHERE discord_id=?", (ID1,))
    assert ledger.award_points(conn, ID1, 50, "event", 9) == (0, 50)


def test_manual_award_needs_reason(conn):
    members.link_rsn(conn, ID1, "Alpha", ID1)
    with pytest.raises(ledger.LedgerError):
        ledger.award_points(conn, ID1, 10, "", 9)


def test_leaving_forfeits_points_keeps_history_and_resets_strikes(conn):
    members.link_rsn(conn, ID1, "Alpha", ID1)
    ledger.award_points(conn, ID1, 100, "x", 9)
    rules.add_strike(conn, ID1, "spam", 9)
    members.remove_member(conn, ID1, 9, "left", "portal")
    row = members.get_member(conn, ID1)
    assert (row["status"], row["ranking_points"], row["spending_points"]) == ("left", 0, 0)
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM points_ledger")]
    assert kinds == ["award", "forfeit"]
    members.link_rsn(conn, ID1, "Alpha", ID1)  # rejoin
    number, _ = rules.add_strike(conn, ID1, "again", 9)
    assert number == 1  # new period starts with zero strikes
    assert conn.execute("SELECT COUNT(*) FROM warnings").fetchone()[0] == 2  # old strike retained


def test_strike_outcomes():
    assert rules.strike_outcome(1).action == "verbal_warning"
    assert rules.strike_outcome(2).action == "timeout_24h"
    assert rules.strike_outcome(3).action == "permanent_ban"


def test_parse_duration():
    assert rules.parse_duration("90m").total_seconds() == 5400
    with pytest.raises(ValueError):
        rules.parse_duration("soon")


def test_join_date_validation():
    assert members.parse_join_date("03-27-2020") == "2020-03-27"
    with pytest.raises(members.MemberError):
        members.parse_join_date("2020-03-27")
    with pytest.raises(members.MemberError):
        members.parse_join_date("01-01-2999")


def test_backup_writes_both_copies_and_flags_missing_secondary(conn, tmp_path):
    assert backup.run_backup(conn, tmp_path / "a", tmp_path / "b") == []
    assert list((tmp_path / "a").glob("*.sqlite3")) and list((tmp_path / "b").glob("*.sqlite3"))
    errors = backup.run_backup(conn, tmp_path / "a", None)
    assert errors and conn.execute("SELECT status FROM heartbeats WHERE name='backup'").fetchone()[0] == "error"


def test_audit_purge_keeps_recent(conn):
    conn.execute(
        "INSERT INTO audit_log(created_at,source,category,action) VALUES('2000-01-01T00:00:00Z','bot','x','old')"
    )
    conn.execute("INSERT INTO audit_log(source,category,action) VALUES('bot','x','new')")
    assert purge_old_audit(conn) == 1
