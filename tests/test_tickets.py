"""Ticket lifecycle, local archive and safe download paths."""

import pytest

from dark_asylum import tickets


def _open(conn):
    """Create a ticket and return its id."""
    return tickets.create_ticket(conn, 10, "General support", 555)


def test_claim_takeover_and_double_claim(conn):
    tid = _open(conn)
    assert tickets.claim_ticket(conn, tid, 1) is None  # first claim
    with pytest.raises(tickets.TicketError):
        tickets.claim_ticket(conn, tid, 1)  # same person twice
    assert tickets.claim_ticket(conn, tid, 2) == 1  # takeover reports old claimant
    events = [r["event"] for r in conn.execute("SELECT event FROM ticket_log ORDER BY id")]
    assert events == ["opened", "claimed", "takeover"]


def test_close_once_only(conn):
    tid = _open(conn)
    tickets.close_ticket(conn, tid, 1)
    with pytest.raises(tickets.TicketError):
        tickets.close_ticket(conn, tid, 1)
    with pytest.raises(tickets.TicketError):
        tickets.claim_ticket(conn, tid, 2)  # closed tickets cannot be claimed


def test_archive_writes_files_and_relative_index(conn, tmp_path):
    tid = _open(conn)
    msgs = [
        {
            "author_id": 10,
            "author_name": "a",
            "created_at": "t",
            "content": "hi",
            "attachments": [("../evil.png", b"data")],
        }
    ]
    path = tickets.archive_ticket(conn, tmp_path, tid, msgs)
    assert "hi" in path.read_text()
    row = conn.execute("SELECT * FROM ticket_attachments").fetchone()
    assert not row["stored_path"].startswith("/") and ".." not in row["stored_path"]
    assert tickets.resolve_archive_path(tmp_path, row["stored_path"]).read_bytes() == b"data"
    tickets.set_dm_status(conn, tid, False)
    assert conn.execute("SELECT dm_status FROM ticket_archives").fetchone()[0] == "failed"


def test_resolve_blocks_traversal(tmp_path):
    (tmp_path / "arch").mkdir()
    (tmp_path / "secret.txt").write_text("x")
    assert tickets.resolve_archive_path(tmp_path / "arch", "../secret.txt") is None
    assert tickets.resolve_archive_path(tmp_path / "arch", "/etc/passwd") is None


def test_sanitize_filename():
    assert "/" not in tickets.sanitize_filename("../a/b.png")
    assert tickets.sanitize_filename("")
