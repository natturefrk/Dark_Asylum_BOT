"""Shared fixtures: an in-memory database and a configured portal test client."""

from __future__ import annotations

import pyotp
import pytest
from cryptography.fernet import Fernet

from dark_asylum import security
from dark_asylum.config import Settings
from dark_asylum.db import connect, migrate
from dark_asylum.portal.app import create_app

ADMIN_A = 111111111111111111
ADMIN_B = 222222222222222222
PASSWORD = "Correct-Horse-Battery-9"


@pytest.fixture()
def conn(tmp_path):
    """A fresh migrated database file for each test."""
    c = connect(tmp_path / "test.sqlite3")
    migrate(c)
    return c


@pytest.fixture()
def settings(tmp_path):
    """Portal settings with throwaway secrets (tests only)."""
    return Settings(
        discord_token="",
        guild_id=None,
        database_path=tmp_path / "test.sqlite3",
        archive_dir=tmp_path / "archive",
        backup_dir=tmp_path / "bk1",
        backup_secondary_dir=tmp_path / "bk2",
        secret_key="x" * 40,
        field_key=Fernet.generate_key().decode(),
        admin_ids=(ADMIN_A, ADMIN_B),
        host="127.0.0.1",
        port=0,
        secure_cookies=False,
        trusted_proxies=0,
    )


@pytest.fixture()
def app(settings, conn):
    """Portal app sharing the test connection."""
    return create_app(settings, conn)


@pytest.fixture()
def secret(app, conn, settings):
    """Enrol admin A and return their TOTP secret."""
    box = security.SecretBox(settings.field_key)
    s = security.start_admin_enrolment(conn, box, ADMIN_A, PASSWORD)
    conn.execute("UPDATE admins SET totp_confirmed=1 WHERE discord_id=?", (ADMIN_A,))
    return s


def next_code(secret: str, conn, discord_id: int = ADMIN_A) -> str:
    """A valid TOTP code for a step newer than the last used one (avoids replay rejection)."""
    import time

    last = conn.execute("SELECT last_totp_step FROM admins WHERE discord_id=?", (discord_id,)).fetchone()[0]
    step = max(last + 1, int(time.time()) // 30)
    return pyotp.TOTP(secret).at(step * 30)
