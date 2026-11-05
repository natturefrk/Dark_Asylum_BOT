"""Member-only surveys with optional anonymity (pure domain logic, no Discord objects).

Anonymous surveys never store who voted: the vote row is keyed by a salted
one-way hash, so even a direct database query cannot reveal the voter.
"""

from __future__ import annotations

import hashlib
import sqlite3

from .db import Tx, audit
from .members import get_member


class SurveyError(ValueError):
    """A rejected survey operation with a user-safe message."""


def create_survey(conn: sqlite3.Connection, question: str, options: list[str], anonymous: bool, actor_id: int) -> int:
    """Create an open survey with 2-10 distinct, non-empty options; returns its id."""
    question = question.strip()
    options = [o.strip() for o in options if o.strip()]
    if not question or len(question) > 300:
        raise SurveyError("A question (max 300 characters) is required.")
    if not 2 <= len(options) <= 10 or len(set(o.lower() for o in options)) != len(options):
        raise SurveyError("Provide 2-10 different options.")
    with Tx(conn):
        sid = conn.execute(
            "INSERT INTO surveys(question,anonymous,created_by) VALUES(?,?,?)", (question, int(anonymous), actor_id)
        ).lastrowid
        conn.executemany("INSERT INTO survey_options(survey_id,label) VALUES(?,?)", [(sid, o[:80]) for o in options])
        audit(conn, "portal", "surveys", "created", actor_id, str(sid), None, question, f"anonymous={anonymous}")
    return sid


def _voter_ref(survey: sqlite3.Row, discord_id: int, salt: str) -> str:
    """Identity key: the plain ID for named surveys, a salted hash for anonymous ones."""
    if not survey["anonymous"]:
        return str(discord_id)
    return hashlib.sha256(f"{salt}:{survey['id']}:{discord_id}".encode()).hexdigest()


def vote(conn: sqlite3.Connection, survey_id: int, option_id: int, discord_id: int, salt: str) -> None:
    """Record or change a member's answer while the survey is open."""
    survey = conn.execute("SELECT * FROM surveys WHERE id=?", (survey_id,)).fetchone()
    if survey is None or survey["status"] != "open":
        raise SurveyError("This survey is closed.")
    member = get_member(conn, discord_id)
    if member is None or member["status"] != "active":
        raise SurveyError("Only clan members can answer surveys.")
    if (
        conn.execute("SELECT 1 FROM survey_options WHERE id=? AND survey_id=?", (option_id, survey_id)).fetchone()
        is None
    ):
        raise SurveyError("Unknown option.")
    # REPLACE implements "members can change their answer".
    conn.execute(
        "INSERT OR REPLACE INTO survey_votes(survey_id,voter_ref,option_id) VALUES(?,?,?)",
        (survey_id, _voter_ref(survey, discord_id, salt), option_id),
    )


def results(conn: sqlite3.Connection, survey_id: int) -> list[sqlite3.Row]:
    """Live tallies per option (counts only, never identities)."""
    return conn.execute(
        "SELECT o.id, o.label, COUNT(v.voter_ref) AS votes FROM survey_options o "
        "LEFT JOIN survey_votes v ON v.option_id=o.id WHERE o.survey_id=? GROUP BY o.id ORDER BY o.id",
        (survey_id,),
    ).fetchall()


def close_survey(conn: sqlite3.Connection, survey_id: int, actor_id: int) -> None:
    """Close a survey manually; results remain visible."""
    with Tx(conn):
        row = conn.execute("SELECT status FROM surveys WHERE id=?", (survey_id,)).fetchone()
        if row is None or row["status"] != "open":
            raise SurveyError("That survey is not open.")
        conn.execute("UPDATE surveys SET status='closed' WHERE id=?", (survey_id,))
        audit(conn, "portal", "surveys", "closed", actor_id, str(survey_id), "open", "closed")
