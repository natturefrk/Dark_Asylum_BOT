"""Authentication primitives: password hashing, TOTP MFA, backup codes, setup tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from datetime import timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet

from .db import Tx, audit, iso, now_iso, utcnow

MIN_PASSWORD_LENGTH = 14
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
BACKUP_CODE_COUNT = 10

_hasher = PasswordHasher()  # Argon2id with library defaults
# Hash of a random value, verified when the account is unknown, so response
# time does not reveal whether an account exists.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def check_password_strength(password: str) -> None:
    """Reject weak passwords (length plus basic variety)."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len({c for c in password}) < 6 or password.lower() == password or password.isalpha():
        raise ValueError("Use a mix of upper/lower case letters and numbers or symbols.")


def hash_password(password: str) -> str:
    """Hash a password with Argon2id."""
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Constant-effort password check; unknown accounts still burn a hash."""
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerificationError, InvalidHashError):
        return False


class SecretBox:
    """Encrypts small secrets (TOTP seeds) at rest with a key from the environment."""

    def __init__(self, key: str):
        """Derive the Fernet key from the configured field key."""
        self._fernet = Fernet(key.encode())

    def encrypt(self, plain: str) -> str:
        """Encrypt a string for storage (used for TOTP secrets)."""
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, token: str) -> str:
        """Reverse encrypt(); raises if the data was tampered with."""
        return self._fernet.decrypt(token.encode()).decode()


def _hash_token(token: str) -> str:
    """Fast SHA-256 is fine for high-entropy random tokens/codes."""
    return hashlib.sha256(token.encode()).hexdigest()


def create_setup_token(conn: sqlite3.Connection, discord_id: int, ttl_minutes: int = 60) -> str:
    """Issue a single-use setup/recovery token (returned once, stored hashed)."""
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM setup_tokens WHERE discord_id=? AND used_at IS NULL", (discord_id,))
    conn.execute(
        "INSERT INTO setup_tokens(discord_id,token_hash,expires_at) VALUES(?,?,?)",
        (discord_id, _hash_token(token), iso(utcnow() + timedelta(minutes=ttl_minutes))),
    )
    return token


def consume_setup_token(conn: sqlite3.Connection, token: str) -> int | None:
    """Validate and burn a setup token; returns the Discord ID it was issued for."""
    with Tx(conn):
        row = conn.execute(
            "SELECT id, discord_id FROM setup_tokens WHERE token_hash=? AND used_at IS NULL AND expires_at > ?",
            (_hash_token(token), now_iso()),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE setup_tokens SET used_at=? WHERE id=?", (now_iso(), row["id"]))
        return row["discord_id"]


def start_admin_enrolment(conn: sqlite3.Connection, box: SecretBox, discord_id: int, password: str) -> str:
    """(Re)create an admin with a fresh password and unconfirmed TOTP secret.

    Replaces any previous credentials for that Discord ID, which is how
    recovery works. Returns the new base32 TOTP secret.
    """
    check_password_strength(password)
    secret = pyotp.random_base32()
    with Tx(conn):
        conn.execute("DELETE FROM admins WHERE discord_id=?", (discord_id,))
        conn.execute(
            "INSERT INTO admins(discord_id,password_hash,totp_secret_enc) VALUES(?,?,?)",
            (discord_id, hash_password(password), box.encrypt(secret)),
        )
        audit(conn, "portal", "admin", "enrolment_started", discord_id, str(discord_id))
    return secret


def generate_backup_codes(conn: sqlite3.Connection, admin_id: int) -> list[str]:
    """Replace the admin's backup codes with a fresh set (shown once)."""
    codes = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(BACKUP_CODE_COUNT)]
    with Tx(conn):
        conn.execute("DELETE FROM backup_codes WHERE admin_id=?", (admin_id,))
        conn.executemany(
            "INSERT INTO backup_codes(admin_id,code_hash) VALUES(?,?)",
            [(admin_id, _hash_token(c)) for c in codes],
        )
    return codes


def verify_second_factor(conn: sqlite3.Connection, box: SecretBox, admin: sqlite3.Row, code: str) -> bool:
    """Check a TOTP code (once per time step) or a single-use backup code."""
    code = code.strip().replace(" ", "")
    totp = pyotp.TOTP(box.decrypt(admin["totp_secret_enc"]))
    if code.isdigit() and len(code) == 6:
        now = int(time.time())
        # Accept the previous/next window for clock drift, but never reuse a step.
        for offset in (-1, 0, 1):
            step = now // 30 + offset
            if hmac.compare_digest(totp.at(step * 30), code) and step > admin["last_totp_step"]:
                conn.execute("UPDATE admins SET last_totp_step=? WHERE id=?", (step, admin["id"]))
                return True
        return False
    # Otherwise treat it as a backup code.
    with Tx(conn):
        row = conn.execute(
            "SELECT id FROM backup_codes WHERE admin_id=? AND code_hash=? AND used_at IS NULL",
            (admin["id"], _hash_token(code.lower())),
        ).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE backup_codes SET used_at=? WHERE id=?", (now_iso(), row["id"]))
        return True


def is_locked(admin: sqlite3.Row | None) -> bool:
    """True while the account is in its brute-force lockout window."""
    return bool(admin and admin["locked_until"] and admin["locked_until"] > now_iso())


def record_failure(conn: sqlite3.Connection, admin: sqlite3.Row) -> None:
    """Count a failed attempt and lock the account after too many."""
    attempts = admin["failed_attempts"] + 1
    locked = None
    if attempts >= MAX_FAILED_ATTEMPTS:
        locked = iso(utcnow() + timedelta(minutes=LOCKOUT_MINUTES))
        attempts = 0
        audit(conn, "portal", "admin", "account_locked", admin["discord_id"], str(admin["discord_id"]))
    conn.execute("UPDATE admins SET failed_attempts=?, locked_until=? WHERE id=?", (attempts, locked, admin["id"]))


def record_success(conn: sqlite3.Connection, admin: sqlite3.Row) -> None:
    """Clear failure counters after a fully authenticated login."""
    conn.execute("UPDATE admins SET failed_attempts=0, locked_until=NULL WHERE id=?", (admin["id"],))
