"""Reaction-role mapping rules."""

import pytest

from dark_asylum import reaction_roles as rr


def _problem(conn, name="Fun", perms=None, default=False, above=False, rid=5):
    """Shortcut for role_problem with safe defaults."""
    return rr.role_problem(conn, rid, name, perms or {}, default, above)


def test_safe_role_allowed(conn):
    assert _problem(conn) is None


def test_blocked_roles(conn):
    assert _problem(conn, default=True)
    assert _problem(conn, above=True)
    assert _problem(conn, perms={"administrator": True})
    assert _problem(conn, name="Member")  # protected clan role (by name)
    assert _problem(conn, name="sr_leadership")


def test_mapping_add_duplicate_delete(conn):
    rr.add_mapping(conn, 1, 2, "🔥", 3, 9)
    with pytest.raises(rr.ReactionRoleError):
        rr.add_mapping(conn, 1, 2, "🔥", 4, 9)
    rr.add_mapping(conn, 1, 2, "🌊", 4, 9)  # several emoji per message allowed
    row = rr.lookup(conn, 2, "🔥")
    assert row["role_id"] == 3 and row["status"] == "pending"
    rr.delete_mapping(conn, row["id"], 9)
    assert rr.lookup(conn, 2, "🔥") is None
    with pytest.raises(rr.ReactionRoleError):
        rr.delete_mapping(conn, 999, 9)
