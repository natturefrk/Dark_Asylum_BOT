"""Operator command line: ``python -m dark_asylum.cli <command>``.

Commands run on the server only; they are how the two designated admins are
bootstrapped or recovered, so server access is the root of trust.
"""

from __future__ import annotations

import argparse
import sys

from . import backup, security
from .config import ConfigError, load_settings
from .db import audit, connect, migrate, purge_old_audit


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the chosen sub-command."""
    parser = argparse.ArgumentParser(prog="dark_asylum.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create/upgrade the database schema")
    tok = sub.add_parser("setup-token", help="issue a one-time portal setup/recovery token")
    tok.add_argument("discord_id", type=int, help="one of PORTAL_ADMIN_DISCORD_IDS")
    sub.add_parser("backup", help="run a backup now")
    sub.add_parser("purge-audit", help="delete audit entries older than 90 days")
    sub.add_parser("portal", help="run the portal with the production WSGI server")
    sub.add_parser("bot", help="run the Discord bot")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(require_portal=args.cmd in {"portal", "setup-token"}, require_bot=args.cmd == "bot")
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "portal":
        from waitress import serve

        from .portal.app import create_app

        serve(create_app(settings), host=settings.host, port=settings.port)
        return 0
    if args.cmd == "bot":
        from .bot.main import run_bot

        run_bot(settings)
        return 0

    conn = connect(settings.database_path)
    migrate(conn)
    if args.cmd == "init-db":
        print("Database ready.")
    elif args.cmd == "setup-token":
        if args.discord_id not in settings.admin_ids:
            print("That Discord ID is not one of the two designated admins.", file=sys.stderr)
            return 1
        token = security.create_setup_token(conn, args.discord_id)
        audit(conn, "system", "admin", "setup_token_issued", None, str(args.discord_id))
        print(f"Setup token (valid 1 hour, single use):\n{token}\nOpen /setup on the portal and paste it.")
    elif args.cmd == "backup":
        errors = backup.run_backup(conn, settings.backup_dir, settings.backup_secondary_dir)
        for e in errors:
            print(f"BACKUP PROBLEM: {e}", file=sys.stderr)
        return 1 if errors else 0
    elif args.cmd == "purge-audit":
        print(f"Removed {purge_old_audit(conn)} old audit entries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
