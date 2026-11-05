"""Environment-driven configuration.

Secrets (tokens, keys) are read from the environment only; nothing sensitive is
hard-coded and no unsafe default credentials exist.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unsafe."""


def _bool(value: str | None, default: bool) -> bool:
    """Parse a human friendly boolean environment value."""
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Immutable bundle of every environment-level setting."""

    discord_token: str
    guild_id: int | None
    database_path: Path
    archive_dir: Path
    backup_dir: Path
    backup_secondary_dir: Path | None
    secret_key: str
    field_key: str
    admin_ids: tuple[int, ...]
    host: str
    port: int
    secure_cookies: bool
    trusted_proxies: int


def load_settings(require_portal: bool = False, require_bot: bool = False) -> Settings:
    """Read settings from ``.env``/environment and validate what the caller needs.

    ``require_portal`` enforces the secrets the web portal cannot run without;
    ``require_bot`` enforces the Discord token.
    """
    load_dotenv()
    env = os.environ.get

    # The two designated portal admins; anything other than exactly two is unsafe.
    raw_ids = [p.strip() for p in (env("PORTAL_ADMIN_DISCORD_IDS") or "").split(",") if p.strip()]
    try:
        admin_ids = tuple(int(p) for p in raw_ids)
    except ValueError as exc:
        raise ConfigError("PORTAL_ADMIN_DISCORD_IDS must be numeric Discord IDs") from exc

    settings = Settings(
        discord_token=env("DISCORD_TOKEN", ""),
        guild_id=int(env("DISCORD_GUILD_ID")) if env("DISCORD_GUILD_ID") else None,
        database_path=Path(env("DATABASE_PATH", "data/dark_asylum.sqlite3")),
        archive_dir=Path(env("ARCHIVE_DIR", "data/archive")),
        backup_dir=Path(env("BACKUP_DIR", "backups/primary")),
        backup_secondary_dir=Path(env("BACKUP_SECONDARY_DIR")) if env("BACKUP_SECONDARY_DIR") else None,
        secret_key=env("PORTAL_SECRET_KEY", ""),
        field_key=env("FIELD_ENCRYPTION_KEY", ""),
        admin_ids=admin_ids,
        host=env("PORTAL_HOST", "127.0.0.1"),
        port=int(env("PORTAL_PORT", "8080")),
        secure_cookies=_bool(env("PORTAL_SECURE_COOKIES"), True),
        trusted_proxies=int(env("PORTAL_TRUSTED_PROXIES", "1")),
    )

    if require_bot and not settings.discord_token:
        raise ConfigError("DISCORD_TOKEN is not set")
    if require_portal:
        if len(settings.secret_key) < 32:
            raise ConfigError("PORTAL_SECRET_KEY must be set to at least 32 characters")
        if not settings.field_key:
            raise ConfigError("FIELD_ENCRYPTION_KEY must be set (see .env.example)")
        if len(set(settings.admin_ids)) != 2:
            raise ConfigError("PORTAL_ADMIN_DISCORD_IDS must list exactly two distinct IDs")
    return settings
