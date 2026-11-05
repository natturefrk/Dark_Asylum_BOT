"""Flask application factory for the Dark Asylum admin portal.

Security model (summarised):
* Only the two Discord IDs in ``PORTAL_ADMIN_DISCORD_IDS`` can ever hold an account.
* Every login needs password + authenticator code (or a single-use backup code).
* Every state-changing request (POST) needs CSRF *and* a fresh password + MFA
  confirmation ("step-up"), enforced centrally in ``before_request``.
* Sessions are HTTP-only, SameSite=Strict, Secure cookies that expire after
  30 minutes idle; the browser warns 2 minutes before.
"""

from __future__ import annotations

import io
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from datetime import date, timedelta
from urllib.parse import urlparse

import qrcode
import qrcode.image.svg
from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from .. import (
    bossing,
    citadel,
    competitions,
    events,
    giveaways,
    ledger,
    members,
    milestones,
    purge,
    reaction_roles,
    redemptions,
    rs3sync,
    rules,
    security,
    surveys,
    tickets,
)
from ..backup import run_backup
from ..config import Settings
from ..db import audit, connect, get_setting, migrate, set_setting, utcnow

IDLE_SECONDS = 30 * 60  # sign out after 30 idle minutes
WARN_SECONDS = 2 * 60  # warn this long before sign-out

# Endpoints whose POST must NOT demand step-up confirmation (they authenticate
# or manage the session itself).
STEP_UP_EXEMPT = {"login", "setup", "logout", "keepalive"}

# Settings an admin may edit, each with a validator returning the cleaned value.


def _v_prefix(v: str) -> str:
    """Validate a command prefix against the allowed set."""
    if v not in rules.VALID_PREFIXES:
        raise ValueError("Prefix must be one of: " + " ".join(rules.VALID_PREFIXES))
    return v


def _v_text(v: str) -> str:
    """Validate short required text (1-64 characters after trimming)."""
    v = v.strip()
    if not v or len(v) > 64:
        raise ValueError("Value must be 1-64 characters.")
    return v


def _v_channel(v: str) -> str:
    """Validate a Discord channel ID (digits only, or blank to unset)."""
    v = v.strip()
    if v and not v.isdigit():
        raise ValueError("Channel must be a numeric Discord channel ID (or blank).")
    return v


def _v_int(v: str) -> str:
    """Validate a non-negative whole number and normalise it."""
    if not v.strip().isdigit():
        raise ValueError("Value must be a whole number.")
    return str(int(v))


def _v_range(low: int, high: int):
    """Build a validator for a whole number within ``low``..``high`` (inclusive)."""

    def check(v: str) -> str:
        """Validate and normalise the number."""
        if not v.strip().isdigit() or not low <= int(v) <= high:
            raise ValueError(f"Value must be a whole number from {low} to {high}.")
        return str(int(v))

    return check


EDITABLE_SETTINGS = {
    "prefix": _v_prefix,
    "cprefix": _v_prefix,
    "clan_name": _v_text,
    "member_role": _v_text,
    "sr_role": _v_text,
    "jr_role": _v_text,
    "command_channel_id": _v_channel,
    "staff_channel_id": _v_channel,
    "ticket_channel_id": _v_channel,
    "ticket_staff_role": _v_text,
    "events_channel_id": _v_channel,
    "giveaway_max_entries": _v_int,
    "ranking_target_points": _v_int,
    "points_to_gp": _v_int,
    "xp_snapshot_hours": _v_range(1, 24),
    "roster_interval_minutes": _v_range(15, 1440),
    "citadel_poll_minutes": _v_range(10, 120),
}


class RateLimiter:
    """Tiny in-memory sliding-window limiter (single process portal)."""

    def __init__(self) -> None:
        """Start with no recorded hits."""
        self.hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, bucket: str, ident: str, limit: int, window: int) -> bool:
        """Record a hit; return False when ``limit`` hits occurred within ``window`` seconds."""
        now = time.monotonic()
        q = self.hits[(bucket, ident)]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def create_app(settings: Settings, conn: sqlite3.Connection | None = None) -> Flask:
    """Build the Flask app. ``conn`` may be injected by tests."""
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=settings.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=settings.secure_cookies,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_NAME="da_session",
        MAX_CONTENT_LENGTH=256 * 1024,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )
    if settings.trusted_proxies:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=settings.trusted_proxies, x_proto=settings.trusted_proxies)

    db = conn or connect(settings.database_path)
    migrate(db)
    box = security.SecretBox(settings.field_key)
    limiter = RateLimiter()
    app.extensions["db"] = db

    # ---- helpers ------------------------------------------------------

    def current_admin() -> sqlite3.Row | None:
        """The signed-in admin row, or None."""
        admin_id = session.get("admin_id")
        if not admin_id:
            return None
        return db.execute("SELECT * FROM admins WHERE id=? AND totp_confirmed=1", (admin_id,)).fetchone()

    def back() -> str:
        """Redirect target after a failed POST: same-origin referrer or dashboard."""
        ref = request.referrer
        if ref and urlparse(ref).netloc == request.host:
            return ref
        return url_for("dashboard")

    def log_change(category: str, action: str, target=None, before=None, after=None, detail=None) -> None:
        """Audit an admin action performed in the portal."""
        audit(db, "portal", category, action, g.admin["discord_id"], target, before, after, detail)

    def recent_changes(category: str | None = None) -> list[sqlite3.Row]:
        """Last submissions, shown at the bottom of each page."""
        if category:
            return db.execute(
                "SELECT * FROM audit_log WHERE source='portal' AND category=? ORDER BY id DESC LIMIT 8",
                (category,),
            ).fetchall()
        return db.execute("SELECT * FROM audit_log WHERE source='portal' ORDER BY id DESC LIMIT 8").fetchall()

    def page(template: str, category: str | None = None, **ctx):
        """Render a page with the shared nav + change history footer."""
        return render_template(template, changes=recent_changes(category), **ctx)

    # ---- request lifecycle -------------------------------------------

    @app.before_request
    def guard():
        """Session expiry, CSRF and step-up confirmation for every request."""
        g.admin = None
        if request.endpoint in (None, "static"):
            return None
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        now = time.time()
        if session.get("admin_id"):
            if now - session.get("last_active", 0) > IDLE_SECONDS:
                session.clear()
                flash("You were signed out after 30 minutes of inactivity.", "warn")
                return redirect(url_for("login"))
            g.admin = current_admin()
            # Polling the status endpoint must not extend the session.
            if request.endpoint != "session_status":
                session["last_active"] = now
        public = {"login", "setup"}
        if g.admin is None and request.endpoint not in public:
            return redirect(url_for("login"))
        if request.method == "POST":
            sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(sent, session["csrf"]):
                abort(400, "Bad CSRF token")
            if request.endpoint not in STEP_UP_EXEMPT and not step_up():
                return redirect(back())
        return None

    def step_up() -> bool:
        """Require the admin's password and a fresh MFA code for every change."""
        admin = g.admin
        if not limiter.allow("stepup", str(admin["id"]), 10, 600):
            flash("Too many confirmation attempts. Wait a few minutes.", "error")
            return False
        if security.is_locked(admin):
            flash("Account temporarily locked.", "error")
            return False
        password = request.form.get("confirm_password", "")
        code = request.form.get("confirm_otp", "")
        if security.verify_password(admin["password_hash"], password) and security.verify_second_factor(
            db, box, admin, code
        ):
            return True
        security.record_failure(db, admin)
        flash("Confirmation failed: check your password and authenticator code.", "error")
        return False

    @app.after_request
    def secure_headers(resp):
        """Strict browser protections on every response."""
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        if settings.secure_cookies:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return resp

    @app.template_filter("gp")
    def fmt_gp(value) -> str:
        """Format numbers with thousands separators."""
        return f"{int(value):,}"

    @app.context_processor
    def inject():
        """Variables every template needs."""
        return {
            "csrf": session.get("csrf", ""),
            "admin": g.get("admin"),
            "idle_seconds": IDLE_SECONDS,
            "warn_seconds": WARN_SECONDS,
        }

    # ---- authentication ----------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        """Password + authenticator code in one step; generic errors only."""
        if request.method == "POST":
            ip = request.remote_addr or "?"
            if not limiter.allow("login", ip, 10, 300):
                flash("Too many attempts. Please wait a few minutes.", "error")
                return render_template("login.html"), 429
            raw_id = request.form.get("discord_id", "").strip()
            admin = None
            if raw_id.isdigit() and int(raw_id) in settings.admin_ids:
                admin = db.execute(
                    "SELECT * FROM admins WHERE discord_id=? AND totp_confirmed=1", (int(raw_id),)
                ).fetchone()
            ok_pw = security.verify_password(
                admin["password_hash"] if admin else None, request.form.get("password", "")
            )
            if (
                admin
                and not security.is_locked(admin)
                and ok_pw
                and security.verify_second_factor(db, box, admin, request.form.get("otp", ""))
            ):
                security.record_success(db, admin)
                session.clear()  # fresh session id prevents fixation
                session.update(admin_id=admin["id"], last_active=time.time(), csrf=secrets.token_urlsafe(32))
                audit(db, "portal", "admin", "login", admin["discord_id"], str(admin["discord_id"]))
                return redirect(url_for("dashboard"))
            if admin and not security.is_locked(admin):
                security.record_failure(db, admin)
            flash("Sign-in failed. Check your details or wait if the account is locked.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        """End the session (a POST so other sites cannot sign the admin out)."""
        session.clear()
        return redirect(url_for("login"))

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        """First-run / recovery enrolment using a one-time server-issued token.

        Step 1 (token + password) creates the account; step 2 confirms the
        authenticator by entering a code, then shows backup codes once.
        """
        if not limiter.allow("setup", request.remote_addr or "?", 10, 600):
            abort(429)
        stage = session.get("setup_stage")
        if request.method == "GET":
            return render_template("setup.html", stage="token", token=request.args.get("token", ""))
        if stage == "confirm":
            admin = db.execute("SELECT * FROM admins WHERE id=?", (session.get("setup_admin"),)).fetchone()
            if admin is None:
                session.pop("setup_stage", None)
                abort(400)
            if security.verify_second_factor(db, box, admin, request.form.get("otp", "")):
                db.execute("UPDATE admins SET totp_confirmed=1 WHERE id=?", (admin["id"],))
                codes = security.generate_backup_codes(db, admin["id"])
                audit(db, "portal", "admin", "enrolment_completed", admin["discord_id"], str(admin["discord_id"]))
                for k in ("setup_stage", "setup_admin"):
                    session.pop(k, None)
                return render_template("setup.html", stage="done", codes=codes)
            flash("That code did not match. Try the next code shown.", "error")
            return render_template("setup.html", stage="confirm", **_qr_context(admin))
        discord_id = security.consume_setup_token(db, request.form.get("token", "").strip())
        if discord_id is None or discord_id not in settings.admin_ids:
            flash("Invalid or expired setup token.", "error")
            return render_template("setup.html", stage="token", token=""), 400
        if request.form.get("password") != request.form.get("password2"):
            flash("Passwords do not match; request a new token.", "error")
            return render_template("setup.html", stage="token", token=""), 400
        try:
            secret = security.start_admin_enrolment(db, box, discord_id, request.form.get("password", ""))
        except ValueError as exc:
            flash(str(exc) + " Request a new token.", "error")
            return render_template("setup.html", stage="token", token=""), 400
        admin = db.execute("SELECT * FROM admins WHERE discord_id=?", (discord_id,)).fetchone()
        session.update(setup_stage="confirm", setup_admin=admin["id"])
        return render_template("setup.html", stage="confirm", **_qr_context(admin, secret))

    def _qr_context(admin: sqlite3.Row, secret: str | None = None) -> dict:
        """Provisioning data for the authenticator app (QR as inline SVG)."""
        import pyotp

        secret = secret or box.decrypt(admin["totp_secret_enc"])
        uri = pyotp.TOTP(secret).provisioning_uri(name=str(admin["discord_id"]), issuer_name="Dark Asylum")
        buf = io.BytesIO()
        qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage).save(buf)
        return {"qr_svg": buf.getvalue().decode(), "secret": secret}

    @app.post("/session/keepalive")
    def keepalive():
        """Explicit "stay signed in" (the guard already refreshed the timer)."""
        return jsonify(remaining=IDLE_SECONDS)

    @app.get("/session/status")
    def session_status():
        """Seconds until idle sign-out, polled by the browser warning script."""
        return jsonify(remaining=max(0, int(IDLE_SECONDS - (time.time() - session.get("last_active", 0)))))

    # ---- pages --------------------------------------------------------

    @app.get("/")
    def dashboard():
        """Overview cards and charts."""
        stats = {
            "bank": ledger.bank_balance(db),
            "members": db.execute("SELECT COUNT(*) c FROM members WHERE status='active'").fetchone()["c"],
            "points": db.execute(
                "SELECT COALESCE(SUM(ranking_points+spending_points),0) c FROM members WHERE status='active'"
            ).fetchone()["c"],
            "strikes": db.execute("SELECT COUNT(*) c FROM warnings").fetchone()["c"],
        }
        series = [
            r["balance_after"]
            for r in db.execute("SELECT balance_after FROM bank_ledger ORDER BY id DESC LIMIT 30").fetchall()
        ][::-1]
        top = ledger.leaderboard(db, 8)
        stats["xp_day"] = rs3sync.total_gain(db, 1)
        stats["xp_week"] = rs3sync.total_gain(db, 7)
        stats["private"] = db.execute(
            "SELECT COUNT(*) c FROM members WHERE status='active' AND profile_private=1"
        ).fetchone()["c"]
        return page(
            "dashboard.html",
            None,
            tab="dashboard",
            stats=stats,
            series=series,
            top=top,
            clan=get_setting(db, "clan_name"),
        )

    @app.get("/members")
    def members_page():
        """List members with search, active/former filter and their RSNs."""
        rows = db.execute("SELECT * FROM members ORDER BY status, rsn").fetchall()
        target = int(get_setting(db, "ranking_target_points", "0") or 0)
        promotion = []
        for m in rows:
            if m["status"] != "active":
                continue
            roster = rs3sync.clan_xp_for(db, m["rsn"]) if m["rsn"] else None
            promotion.append(
                {
                    "rsn": m["rsn"] or str(m["discord_id"]),
                    "joined": m["join_date"],
                    "rank": roster["rank"] if roster else None,
                    "clan_xp": roster["total_xp"] if roster else None,
                    "points": m["ranking_points"],
                    "eligible": bool(target) and m["ranking_points"] >= target and not m["ranking_opt_out"],
                }
            )
        promotion.sort(key=lambda r: (not r["eligible"], -r["points"]))
        return page("members.html", "members", tab="members", rows=rows, promotion=promotion, target=target)

    @app.post("/members/link")
    def members_link():
        """Create/link a member (RSN + optional join date) on behalf of a Discord ID."""
        try:
            did = int(request.form["discord_id"])
            jd = request.form.get("join_date") or None
            members.link_rsn(
                db, did, request.form["rsn"], g.admin["discord_id"], members.parse_join_date(jd) if jd else None
            )
            flash("Member saved.", "ok")
        except (ValueError, KeyError) as exc:
            flash(str(exc) or "Invalid input.", "error")
        return redirect(url_for("members_page"))

    @app.post("/members/<int:did>/remove")
    def members_remove(did: int):
        """Remove a membership profile; history, points and bank ledger are kept."""
        try:
            members.remove_member(
                db,
                did,
                g.admin["discord_id"],
                request.form.get("reason", "removed by admin") or "removed by admin",
                "portal",
            )
            flash("Member profile closed; history and ledgers retained.", "ok")
        except members.MemberError as exc:
            flash(str(exc), "error")
        return redirect(url_for("members_page"))

    @app.post("/members/<int:did>/opt-out")
    def members_opt_out(did: int):
        """Toggle whether points skip the ranking requirement (admin-only)."""
        row = members.get_member(db, did)
        if row is None:
            abort(404)
        new = 0 if row["ranking_opt_out"] else 1
        db.execute("UPDATE members SET ranking_opt_out=? WHERE discord_id=?", (new, did))
        log_change("members", "ranking_opt_out", str(did), str(row["ranking_opt_out"]), str(new))
        flash("Ranking opt-out updated.", "ok")
        return redirect(url_for("members_page"))

    @app.get("/bank")
    def bank_page():
        """Show the bank balance, recent ledger entries and the transaction form."""
        rows = db.execute("SELECT * FROM bank_ledger ORDER BY id DESC LIMIT 50").fetchall()
        return page("bank.html", "bank", tab="bank", rows=rows, balance=ledger.bank_balance(db))

    @app.post("/bank")
    def bank_record():
        """Record a GP deposit or withdrawal."""
        try:
            amount = int(request.form["amount"].replace(",", ""))
            if request.form.get("direction") == "withdraw":
                amount = -abs(amount)
            ledger.record_bank(db, amount, request.form.get("reason", ""), g.admin["discord_id"])
            flash("Transaction recorded.", "ok")
        except (ValueError, KeyError) as exc:
            flash(str(exc) or "Invalid amount.", "error")
        return redirect(url_for("bank_page"))

    @app.get("/points")
    def points_page():
        """Show the leaderboard, award form, ranking opt-outs and the value simulator."""
        awards = db.execute("SELECT * FROM point_awards ORDER BY id").fetchall()
        rows = db.execute(
            "SELECT l.*, m.rsn FROM points_ledger l JOIN members m USING(discord_id) ORDER BY l.id DESC LIMIT 50"
        ).fetchall()
        return page(
            "points.html",
            "points",
            tab="points",
            awards=awards,
            rows=rows,
            rate=int(get_setting(db, "points_to_gp", "100000")),
        )

    @app.post("/points/award")
    def points_award():
        """Manual award: preset or custom amount, reason mandatory."""
        try:
            did = int(request.form["discord_id"])
            amount = int(request.form["amount"])
            ledger.award_points(db, did, amount, request.form.get("reason", ""), g.admin["discord_id"])
            flash("Points awarded.", "ok")
        except (ValueError, KeyError) as exc:
            flash(str(exc) or "Invalid input.", "error")
        return redirect(url_for("points_page"))

    @app.post("/points/presets")
    def points_preset_save():
        """Create or change a preset award amount."""
        name = request.form.get("name", "").strip()
        try:
            amount = int(request.form.get("amount", ""))
            if not name or amount <= 0:
                raise ValueError("Name and a positive amount are required.")
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("points_page"))
        old = db.execute("SELECT amount FROM point_awards WHERE name=?", (name,)).fetchone()
        db.execute(
            "INSERT INTO point_awards(name,amount) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET amount=?",
            (name, amount, amount),
        )
        log_change("points", "preset_saved", name, str(old["amount"]) if old else None, str(amount))
        flash("Preset saved.", "ok")
        return redirect(url_for("points_page"))

    @app.get("/moderation")
    def moderation_page():
        """Show editable bot settings, custom commands and temporary roles."""
        current = {k: get_setting(db, k) for k in EDITABLE_SETTINGS}
        cmds = db.execute("SELECT * FROM custom_commands ORDER BY name").fetchall()
        temp = db.execute("SELECT * FROM temp_roles WHERE removed_at IS NULL ORDER BY expires_at").fetchall()
        warns = db.execute("SELECT * FROM warnings ORDER BY id DESC LIMIT 25").fetchall()
        return page(
            "moderation.html",
            "settings",
            tab="moderation",
            current=current,
            cmds=cmds,
            temp=temp,
            warns=warns,
            prefixes=rules.VALID_PREFIXES,
        )

    @app.post("/moderation/settings")
    def settings_save():
        """Save settings, logging before/after for each changed key."""
        changed = 0
        for key, validate in EDITABLE_SETTINGS.items():
            if key not in request.form:
                continue
            try:
                new = validate(request.form[key])
            except ValueError as exc:
                flash(f"{key}: {exc}", "error")
                continue
            old = get_setting(db, key)
            if new != old:
                set_setting(db, key, new)
                log_change("settings", "setting_changed", key, old, new)
                changed += 1
        flash(f"{changed} setting(s) updated.", "ok")
        return redirect(back())

    @app.post("/moderation/commands")
    def command_save():
        """Create/update a saved-text custom command."""
        name = request.form.get("name", "").strip().lower()
        text = request.form.get("response", "").strip()
        if not name.isalnum() or len(name) > 24 or not text or len(text) > 1500:
            flash("Name must be letters/numbers (max 24); response 1-1500 characters.", "error")
            return redirect(url_for("moderation_page"))
        old = db.execute("SELECT response FROM custom_commands WHERE name=?", (name,)).fetchone()
        db.execute(
            "INSERT INTO custom_commands(name,response) VALUES(?,?) "
            "ON CONFLICT(name) DO UPDATE SET response=excluded.response",
            (name, text),
        )
        log_change("settings", "custom_command_saved", name, old["response"] if old else None, text)
        flash("Custom command saved.", "ok")
        return redirect(url_for("moderation_page"))

    @app.post("/moderation/commands/<name>/delete")
    def command_delete(name: str):
        """Delete a custom command (step-up confirmed, audited)."""
        old = db.execute("SELECT response FROM custom_commands WHERE name=?", (name,)).fetchone()
        db.execute("DELETE FROM custom_commands WHERE name=?", (name,))
        log_change("settings", "custom_command_deleted", name, old["response"] if old else None, None)
        flash("Custom command deleted.", "ok")
        return redirect(url_for("moderation_page"))

    @app.get("/audit")
    def audit_page():
        """Show the retained audit log of important actions."""
        rows = db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200").fetchall()
        return page("audit.html", None, tab="audit", rows=rows)

    @app.get("/health")
    def health_page():
        """Show bot heartbeat age, last backup result and API availability."""
        beats = db.execute("SELECT * FROM heartbeats ORDER BY name").fetchall()
        view = []
        for b in beats:
            age = (utcnow() - events.parse_utc(b["updated_at"])).total_seconds()
            stale = b["name"] == "bot" and age > 120
            view.append(
                {
                    "name": b["name"],
                    "status": "stale" if stale else b["status"],
                    "updated": b["updated_at"],
                    "detail": b["detail"],
                }
            )
        interval_h = rs3sync.snapshot_hours(db)
        last_run = get_setting(db, "xp_snapshot_last_run") or None
        return page("health.html", "backup", tab="health", beats=view, interval_h=interval_h, last_run=last_run)

    @app.post("/health/backup")
    def backup_now():
        """Run a backup immediately and report success or failure loudly."""
        errors = run_backup(db, settings.backup_dir, settings.backup_secondary_dir)
        log_change("backup", "manual_backup", detail="; ".join(errors) or "ok")
        flash(
            "Backup finished." if not errors else "Backup problems: " + "; ".join(errors),
            "ok" if not errors else "error",
        )
        return redirect(url_for("health_page"))

    @app.get("/account")
    def account_page():
        """Show the admin's own security settings and recovery options."""
        left = db.execute(
            "SELECT COUNT(*) c FROM backup_codes WHERE admin_id=? AND used_at IS NULL", (g.admin["id"],)
        ).fetchone()["c"]
        other = [i for i in settings.admin_ids if i != g.admin["discord_id"]][0]
        return page("account.html", "admin", tab="account", codes_left=left, other_id=other)

    @app.post("/account/backup-codes")
    def regen_codes():
        """Replace the admin's backup codes and show the new ones once."""
        codes = security.generate_backup_codes(db, g.admin["id"])
        log_change("admin", "backup_codes_regenerated")
        return render_template("codes.html", codes=codes)

    @app.post("/account/recovery-token")
    def recovery_token():
        """Let one admin issue a recovery token for the other designated admin."""
        other = [i for i in settings.admin_ids if i != g.admin["discord_id"]][0]
        token = security.create_setup_token(db, other)
        log_change("admin", "recovery_token_issued", str(other))
        return render_template("codes.html", token=token)

    @app.get("/xp")
    def xp_page():
        """XP dashboard: top-10 earners per period, clan roster XP, snapshot status and DXP dates."""
        periods = {"Today": 1, "This week": 7, "Last 30 days": 30}
        boards = {label: rs3sync.top_earners(db, days) for label, days in periods.items()}
        roster = db.execute("SELECT * FROM clan_roster ORDER BY total_xp DESC LIMIT 50").fetchall()
        private = db.execute(
            "SELECT rsn FROM members WHERE status='active' AND profile_private=1 ORDER BY rsn"
        ).fetchall()
        return page(
            "xp.html",
            "xp",
            tab="xp",
            boards=boards,
            roster=roster,
            private=private,
            dxp=db.execute("SELECT * FROM dxp_periods ORDER BY starts_on DESC LIMIT 20").fetchall(),
            interval_h=rs3sync.snapshot_hours(db),
            last_run=get_setting(db, "xp_snapshot_last_run") or None,
        )

    @app.post("/xp/dxp")
    def dxp_add():
        """Record a double-XP period (UTC dates). Entered by hand: there is no public DXP feed."""
        start, end = request.form["starts_on"].strip(), request.form["ends_on"].strip()
        note = request.form.get("note", "").strip()[:120]
        try:
            d1, d2 = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError:
            flash("Use dates like 2025-01-31.", "error")
            return redirect(url_for("xp_page"))
        if d2 < d1:
            flash("End date must not be before the start date.", "error")
            return redirect(url_for("xp_page"))
        db.execute(
            "INSERT INTO dxp_periods(starts_on,ends_on,note,created_by) VALUES(?,?,?,?)",
            (d1.isoformat(), d2.isoformat(), note, g.admin["discord_id"]),
        )
        log_change("xp", "dxp_added", None, None, f"{d1} to {d2} {note}".strip())
        flash("Double XP period saved.", "ok")
        return redirect(url_for("xp_page"))

    @app.post("/xp/dxp/<int:pid>/delete")
    def dxp_delete(pid: int):
        """Delete a double-XP period."""
        row = db.execute("SELECT * FROM dxp_periods WHERE id=?", (pid,)).fetchone()
        if row:
            db.execute("DELETE FROM dxp_periods WHERE id=?", (pid,))
            log_change("xp", "dxp_deleted", str(pid), f"{row['starts_on']} to {row['ends_on']}", None)
            flash("Period deleted.", "ok")
        return redirect(url_for("xp_page"))

    @app.get("/api/members/search")
    def member_search():
        """Autocomplete endpoint for RSN / Discord ID pickers."""
        q = request.args.get("q", "")
        if len(q) < 1:
            return jsonify([])
        return jsonify(
            [{"id": str(r["discord_id"]), "rsn": r["rsn"], "status": r["status"]} for r in members.search(db, q)]
        )

    # ---- tickets ------------------------------------------------------

    @app.get("/tickets")
    def tickets_page():
        """Ticket categories, settings and the list of tickets with archive links."""
        rows = db.execute(
            "SELECT t.*, a.transcript_path, a.dm_status, a.message_count FROM tickets t "
            "LEFT JOIN ticket_archives a ON a.ticket_id=t.id ORDER BY t.id DESC LIMIT 100"
        ).fetchall()
        types = db.execute("SELECT * FROM ticket_types ORDER BY id").fetchall()
        current = {k: get_setting(db, k) for k in ("ticket_channel_id", "ticket_staff_role")}
        atts = db.execute("SELECT * FROM ticket_attachments ORDER BY id").fetchall()
        return page("tickets.html", "tickets", tab="tickets", rows=rows, types=types, current=current, atts=atts)

    @app.post("/tickets/types")
    def ticket_type_save():
        """Create or update a ticket category."""
        name = request.form.get("name", "").strip()
        desc = request.form.get("description", "").strip()
        if not name or len(name) > 50 or not desc or len(desc) > 100:
            flash("Name (max 50) and description (max 100) are required.", "error")
            return redirect(url_for("tickets_page"))
        old = db.execute("SELECT description FROM ticket_types WHERE name=?", (name,)).fetchone()
        db.execute(
            "INSERT INTO ticket_types(name,description) VALUES(?,?) "
            "ON CONFLICT(name) DO UPDATE SET description=excluded.description",
            (name, desc),
        )
        log_change("tickets", "type_saved", name, old["description"] if old else None, desc)
        flash("Ticket type saved.", "ok")
        return redirect(url_for("tickets_page"))

    @app.post("/tickets/types/<int:type_id>/toggle")
    def ticket_type_toggle(type_id: int):
        """Enable or disable a category (history keeps its name)."""
        row = db.execute("SELECT * FROM ticket_types WHERE id=?", (type_id,)).fetchone()
        if row is None:
            abort(404)
        new = 0 if row["enabled"] else 1
        db.execute("UPDATE ticket_types SET enabled=? WHERE id=?", (new, type_id))
        log_change("tickets", "type_toggled", row["name"], str(row["enabled"]), str(new))
        return redirect(url_for("tickets_page"))

    @app.get("/tickets/<int:ticket_id>/transcript")
    def ticket_transcript(ticket_id: int):
        """Download a transcript by ticket id; filesystem paths never reach the browser."""
        row = db.execute("SELECT transcript_path FROM ticket_archives WHERE ticket_id=?", (ticket_id,)).fetchone()
        path = tickets.resolve_archive_path(settings.archive_dir, row["transcript_path"]) if row else None
        if path is None:
            abort(404)
        return send_file(path, mimetype="text/plain", as_attachment=True, download_name=f"ticket-{ticket_id}.txt")

    @app.get("/tickets/attachments/<int:att_id>")
    def ticket_attachment(att_id: int):
        """Download an archived attachment as an opaque binary (never rendered inline)."""
        row = db.execute("SELECT * FROM ticket_attachments WHERE id=?", (att_id,)).fetchone()
        path = tickets.resolve_archive_path(settings.archive_dir, row["stored_path"]) if row else None
        if path is None:
            abort(404)
        return send_file(
            path,
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=tickets.sanitize_filename(row["original_name"]),
        )

    # ---- reaction roles -----------------------------------------------

    @app.get("/reaction-roles")
    def reaction_roles_page():
        """List reaction-role mappings with the bot-reported status."""
        rows = db.execute("SELECT * FROM reaction_roles ORDER BY id").fetchall()
        return page("reaction_roles.html", "reaction_roles", tab="reaction", rows=rows)

    @app.post("/reaction-roles")
    def reaction_role_add():
        """Map an emoji on a message to a role (the bot rejects privileged roles)."""
        try:
            reaction_roles.add_mapping(
                db,
                int(request.form["channel_id"]),
                int(request.form["message_id"]),
                request.form.get("emoji", ""),
                int(request.form["role_id"]),
                g.admin["discord_id"],
            )
            flash("Mapping saved; the bot validates it within a minute.", "ok")
        except (ValueError, KeyError) as exc:
            flash(str(exc) or "Channel, message and role must be numeric IDs.", "error")
        return redirect(url_for("reaction_roles_page"))

    @app.post("/reaction-roles/<int:mapping_id>/delete")
    def reaction_role_delete(mapping_id: int):
        """Remove a mapping (roles already granted are left alone)."""
        try:
            reaction_roles.delete_mapping(db, mapping_id, g.admin["discord_id"])
            flash("Mapping removed.", "ok")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("reaction_roles_page"))

    # ---- citadel ------------------------------------------------------

    @app.get("/citadel")
    def citadel_page():
        """Show the current cycle checklist, the record form and archived cycles."""
        cycle = citadel.current_cycle(db)
        entries = db.execute(
            "SELECT e.*, m.rsn FROM citadel_entries e JOIN members m USING(discord_id) "
            "WHERE e.cycle_id=? ORDER BY m.rsn",
            (cycle,),
        ).fetchall()
        history = db.execute(
            "SELECT * FROM citadel_cycles WHERE ended_at IS NOT NULL ORDER BY id DESC LIMIT 20"
        ).fetchall()
        return page("citadel.html", "citadel", tab="citadel", cycle=cycle, entries=entries, history=history)

    @app.post("/citadel/mark")
    def citadel_mark():
        """Record that a member visited or capped; points are awarded once per cycle."""
        try:
            done = citadel.mark(
                db, int(request.form["discord_id"]), request.form.get("result") == "capped", g.admin["discord_id"]
            )
            flash("Recorded. " + (", ".join(done) or "No new points."), "ok")
        except (ValueError, KeyError) as exc:
            flash(str(exc) or "Invalid input.", "error")
        return redirect(url_for("citadel_page"))

    @app.post("/citadel/reset")
    def citadel_reset():
        """Archive the current cycle's results and begin a new one."""
        citadel.reset_cycle(db, g.admin["discord_id"])
        flash("Cycle archived; a new cycle has started.", "ok")
        return redirect(url_for("citadel_page"))

    # ---- clan activities: events, competitions, giveaways, surveys ----

    def attempt(action, ok: str, target: str):
        """Run a domain action; flash its success text or its (user-safe) error, then go to ``target``."""
        try:
            result = action()
            flash(ok + (f" {result}" if isinstance(result, str) else ""), "ok")
        except (ValueError, KeyError) as exc:  # domain errors all derive from ValueError
            flash(str(exc) or "Invalid input.", "error")
        return redirect(url_for(target))

    def num(name: str) -> int:
        """Read a whole number form field (commas allowed)."""
        return int(request.form[name].replace(",", "").strip() or 0)

    @app.get("/activities")
    def activities_page():
        """One page with tabs for events, competitions and giveaways."""
        q = db.execute
        return page(
            "activities.html",
            None,
            tab="activities",
            events=q("SELECT * FROM events ORDER BY starts_at DESC LIMIT 50").fetchall(),
            comps=q("SELECT * FROM competitions ORDER BY id DESC LIMIT 30").fetchall(),
            entries=q(
                "SELECT e.*, c.status AS comp_status FROM competition_entries e "
                "JOIN competitions c ON c.id=e.competition_id ORDER BY e.id"
            ).fetchall(),
            gives=q("SELECT * FROM giveaways ORDER BY id DESC LIMIT 30").fetchall(),
            balance=ledger.bank_balance(db),
        )

    @app.post("/events")
    def event_create():
        """Schedule a one-time or recurring event (UTC)."""
        f = request.form
        return attempt(
            lambda: events.create_event(
                db,
                f.get("title", ""),
                f.get("starts_at", ""),
                num("recurrence_days"),
                bool(f.get("remind_24h")),
                bool(f.get("remind_1h")),
                g.admin["discord_id"],
                f.get("description", ""),
            ),
            "Event scheduled.",
            "activities_page",
        )

    @app.post("/events/<int:event_id>/cancel")
    def event_cancel(event_id: int):
        """Cancel an event and its future repeats."""
        return attempt(
            lambda: events.cancel_event(db, event_id, g.admin["discord_id"]), "Event cancelled.", "activities_page"
        )

    @app.post("/events/attendance")
    def event_attendance():
        """Confirm that a member attended; points are awarded once."""
        f = request.form
        return attempt(
            lambda: events.confirm_attendance(
                db, num("event_id"), f.get("occurrence", "").strip(), num("discord_id"), g.admin["discord_id"]
            ),
            "Attendance confirmed.",
            "activities_page",
        )

    @app.post("/competitions")
    def competition_create():
        """Create a draft competition."""
        f = request.form
        return attempt(
            lambda: competitions.create(
                db,
                f.get("title", ""),
                f.get("kind", ""),
                f.get("skill", ""),
                bool(f.get("rewarded")),
                g.admin["discord_id"],
            ),
            "Competition created (draft).",
            "activities_page",
        )

    @app.post("/competitions/<int:cid>/status")
    def competition_status(cid: int):
        """Advance the competition lifecycle."""
        return attempt(
            lambda: competitions.set_status(db, cid, request.form.get("status", ""), g.admin["discord_id"]),
            "Status updated.",
            "activities_page",
        )

    @app.post("/competitions/proof/<int:entry_id>")
    def competition_proof(entry_id: int):
        """Approve or reject a submitted proof."""
        return attempt(
            lambda: competitions.review_proof(
                db, entry_id, request.form.get("decision") == "approve", g.admin["discord_id"]
            ),
            "Proof reviewed.",
            "activities_page",
        )

    @app.post("/competitions/<int:cid>/finish")
    def competition_finish(cid: int):
        """Finish after review; the admin names the 1st-3rd place entries (this also resolves ties)."""
        places = {n: int(request.form[f"place{n}"]) for n in (1, 2, 3) if request.form.get(f"place{n}", "").strip()}
        return attempt(
            lambda: "; ".join(competitions.finish(db, cid, places, g.admin["discord_id"])) or "No points were due.",
            "Competition finished.",
            "activities_page",
        )

    @app.post("/giveaways")
    def giveaway_create():
        """Publish a giveaway (the bank must be able to fund the prize)."""
        f = request.form
        return attempt(
            lambda: giveaways.create_giveaway(
                db,
                f.get("title", ""),
                num("prize_gp"),
                num("entry_cost"),
                f.get("channel_id", "").strip(),
                g.admin["discord_id"],
            ),
            "Giveaway created; the bot will announce it.",
            "activities_page",
        )

    @app.post("/giveaways/<int:gid>/<action>")
    def giveaway_action(gid: int, action: str):
        """Cancel (refund), draw a winner, or record the bank payout."""
        admin = g.admin["discord_id"]
        actions = {
            "cancel": lambda: f"{giveaways.cancel(db, gid, admin):,} points refunded.",
            "draw": lambda: f"Winner: {giveaways.draw(db, gid, admin)}.",
            "payout": lambda: giveaways.record_payout(db, gid, admin),
        }
        if action not in actions:
            abort(404)
        return attempt(actions[action], "Done.", "activities_page")

    @app.get("/surveys")
    def surveys_page():
        """Surveys with live results (counts only; anonymous surveys hold no identities)."""
        rows = db.execute("SELECT * FROM surveys ORDER BY id DESC LIMIT 30").fetchall()
        tallies = {r["id"]: surveys.results(db, r["id"]) for r in rows}
        return page("surveys.html", "surveys", tab="surveys", rows=rows, tallies=tallies)

    @app.post("/surveys")
    def survey_create():
        """Create a survey; options come one per line."""
        f = request.form
        return attempt(
            lambda: surveys.create_survey(
                db,
                f.get("question", ""),
                f.get("options", "").splitlines(),
                bool(f.get("anonymous")),
                g.admin["discord_id"],
            ),
            "Survey created; the bot will publish it.",
            "surveys_page",
        )

    @app.post("/surveys/<int:sid>/close")
    def survey_close(sid: int):
        """Close a survey manually."""
        return attempt(lambda: surveys.close_survey(db, sid, g.admin["discord_id"]), "Survey closed.", "surveys_page")

    # ---- rewards: milestones, redemptions, bossing gear goals, message purge ----

    @app.get("/rewards")
    def rewards_page():
        """Tabbed page for XP milestones, redemption windows/requests, gear goals and purge jobs."""
        q = db.execute
        return page(
            "rewards.html",
            None,
            tab="rewards",
            ms=q("SELECT * FROM xp_milestones ORDER BY scope, threshold").fetchall(),
            windows=q("SELECT * FROM redemption_windows ORDER BY day DESC LIMIT 12").fetchall(),
            reds=q("SELECT * FROM redemptions ORDER BY id DESC LIMIT 50").fetchall(),
            items=q("SELECT * FROM bossing_items ORDER BY name").fetchall(),
            goals=q("SELECT * FROM bossing_goals ORDER BY id DESC LIMIT 50").fetchall(),
            purges=q("SELECT * FROM purge_jobs ORDER BY id DESC LIMIT 20").fetchall(),
            default_discount=bossing.discount_for(db, 0),
        )

    @app.post("/rewards/<area>/<action>")
    def rewards_action(area: str, action: str):
        """Dispatch one confirmed rewards form (password + OTP are checked by the global step-up guard)."""
        f, admin = request.form, g.admin["discord_id"]
        table = {
            ("milestone", "add"): lambda: milestones.add_milestone(
                db, f.get("scope", ""), f.get("skill", ""), num("threshold"), num("points"), admin
            ),
            ("milestone", "toggle"): lambda: milestones.set_enabled(db, num("id"), f.get("enabled") == "1", admin),
            ("window", "add"): lambda: redemptions.add_window(db, f.get("day", ""), f.get("kind", ""), admin),
            ("redeem", "approve"): lambda: redemptions.approve(db, num("id"), admin),
            ("redeem", "reject"): lambda: redemptions.reject(db, num("id"), admin),
            ("redeem", "pay"): lambda: redemptions.pay(db, num("id"), admin),
            ("redeem", "cancel"): lambda: redemptions.cancel_approved(db, num("id"), admin),
            ("item", "save"): lambda: bossing.save_item(
                db, f.get("name", ""), num("price"), f.get("source", "manual"), bool(f.get("confirmed")), admin
            ),
            ("discount", "set"): lambda: bossing.set_member_discount(
                db, num("member"), num("percent") if f.get("percent", "").strip() else None, admin
            ),
            ("goal", "start"): lambda: bossing.start_goal(db, num("member"), num("item"), admin),
            ("goal", "contribute"): lambda: bossing.contribute(db, num("id"), num("amount"), admin),
            ("goal", "fulfill"): lambda: bossing.fulfill(db, num("id"), admin),
            ("goal", "cancel"): lambda: bossing.cancel(db, num("id"), admin),
            ("purge", "queue"): lambda: purge.queue(db, num("channel"), num("days"), admin),
        }
        if (area, action) not in table:
            abort(404)
        return attempt(table[(area, action)], "Done.", "rewards_page")

    return app
