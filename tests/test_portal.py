"""Portal security behaviour: MFA login, CSRF, step-up confirmation, idle timeout, setup."""

import re
import time

from dark_asylum import security
from dark_asylum.db import iso, utcnow
from tests.conftest import ADMIN_A, PASSWORD, next_code


def csrf_of(client, path="/login"):
    """Fetch a page and extract its CSRF token."""
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(client, secret, conn, **over):
    """Log the test admin in with a valid password and OTP (fields can be overridden)."""
    data = {"csrf": csrf_of(client), "discord_id": str(ADMIN_A), "password": PASSWORD, "otp": next_code(secret, conn)}
    data.update(over)
    return client.post("/login", data=data)


def test_requires_login(app):
    assert app.test_client().get("/").status_code == 302


def test_login_needs_password_and_otp(app, conn, secret):
    c = app.test_client()
    assert login(c, secret, conn, password="wrong").status_code == 200
    assert login(c, secret, conn, otp="000000").status_code == 200
    assert c.get("/").status_code == 302
    assert login(c, secret, conn).status_code == 302
    assert c.get("/").status_code == 200


def test_unlisted_discord_id_cannot_login(app, conn, secret):
    assert login(app.test_client(), secret, conn, discord_id="999").status_code == 200


def test_security_headers_and_cookie(app, conn, secret):
    c = app.test_client()
    resp = login(c, secret, conn)
    cookie = resp.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert "default-src 'none'" in c.get("/").headers["Content-Security-Policy"]


def test_csrf_rejected(app, conn, secret):
    c = app.test_client()
    login(c, secret, conn)
    assert c.post("/bank", data={"amount": "5"}).status_code == 400


def test_change_requires_step_up(app, conn, secret):
    c = app.test_client()
    login(c, secret, conn)
    token = csrf_of(c, "/bank")
    base = {"csrf": token, "direction": "deposit", "amount": "1,000", "reason": "test"}
    c.post("/bank", data=base)  # no confirmation: must not apply
    assert conn.execute("SELECT COUNT(*) FROM bank_ledger").fetchone()[0] == 0
    c.post("/bank", data={**base, "confirm_password": "nope", "confirm_otp": "123456"})
    assert conn.execute("SELECT COUNT(*) FROM bank_ledger").fetchone()[0] == 0
    c.post("/bank", data={**base, "confirm_password": PASSWORD, "confirm_otp": next_code(secret, conn)})
    assert conn.execute("SELECT balance_after FROM bank_ledger").fetchone()[0] == 1000
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE category='bank'").fetchone()[0] == 1


def test_otp_cannot_be_replayed(app, conn, secret):
    c = app.test_client()
    code = next_code(secret, conn)
    assert login(c, secret, conn, otp=code).status_code == 302
    assert login(app.test_client(), secret, conn, otp=code).status_code == 200


def test_idle_session_expires(app, conn, secret):
    c = app.test_client()
    login(c, secret, conn)
    with c.session_transaction() as s:
        s["last_active"] = time.time() - 31 * 60
    assert c.get("/").status_code == 302


def test_status_endpoint_reports_remaining(app, conn, secret):
    c = app.test_client()
    login(c, secret, conn)
    with c.session_transaction() as s:
        s["last_active"] = time.time() - 29 * 60
    remaining = c.get("/session/status").get_json()["remaining"]
    assert 0 < remaining <= 60


def test_lockout_after_repeated_failures(app, conn, secret):
    c = app.test_client()
    for _ in range(security.MAX_FAILED_ATTEMPTS):
        login(c, secret, conn, password="bad")
    assert login(c, secret, conn).status_code == 200  # locked even with correct creds


def test_setup_flow_for_designated_admin(app, conn, settings):
    token = security.create_setup_token(conn, ADMIN_A)
    c = app.test_client()
    t = csrf_of(c, "/setup")
    resp = c.post("/setup", data={"csrf": t, "token": token, "password": PASSWORD, "password2": PASSWORD})
    assert resp.status_code == 200 and b"Manual key" in resp.data
    secret = re.search(rb"<code>([A-Z2-7]{16,})</code>", resp.data).group(1).decode()
    t = csrf_of(c, "/setup")
    resp = c.post("/setup", data={"csrf": t, "otp": next_code(secret, conn)})
    assert b"backup codes" in resp.data
    assert conn.execute("SELECT totp_confirmed FROM admins").fetchone()[0] == 1
    assert c.post("/setup", data={"csrf": t, "token": token}).status_code == 400  # token is single use


def test_setup_token_for_other_id_rejected(app, conn):
    token = security.create_setup_token(conn, 555)
    c = app.test_client()
    t = csrf_of(c, "/setup")
    resp = c.post("/setup", data={"csrf": t, "token": token, "password": PASSWORD, "password2": PASSWORD})
    assert resp.status_code == 400


def test_weak_password_rejected():
    import pytest

    with pytest.raises(ValueError):
        security.check_password_strength("short")


def test_every_page_renders(app, conn, secret):
    c = app.test_client()
    login(c, secret, conn)
    for path in [
        "/",
        "/members",
        "/bank",
        "/points",
        "/moderation",
        "/audit",
        "/health",
        "/account",
        "/xp",
        "/tickets",
        "/reaction-roles",
        "/citadel",
        "/activities",
        "/surveys",
        "/api/members/search?q=a",
    ]:
        assert c.get(path).status_code == 200, path


def test_reaction_role_post_needs_step_up(app, conn, secret):
    """Reaction-role changes are blocked without password+OTP confirmation."""
    c = app.test_client()
    login(c, secret, conn)
    token = csrf_of(c, "/reaction-roles")
    form = {"csrf": token, "channel_id": "1", "message_id": "2", "emoji": "x", "role_id": "3"}
    c.post("/reaction-roles", data=form)  # unconfirmed -> rejected
    assert conn.execute("SELECT COUNT(*) FROM reaction_roles").fetchone()[0] == 0
    c.post("/reaction-roles", data={**form, "confirm_password": PASSWORD, "confirm_otp": next_code(secret, conn)})
    assert conn.execute("SELECT COUNT(*) FROM reaction_roles").fetchone()[0] == 1


def test_citadel_post_needs_step_up(app, conn, secret):
    """Citadel marks pay points only after confirmation (3 + 150 total for a cap)."""
    from dark_asylum import members

    members.link_rsn(conn, 7, "Gamma", 7)
    c = app.test_client()
    login(c, secret, conn)
    form = {"csrf": csrf_of(c, "/citadel"), "discord_id": "7", "result": "capped"}
    c.post("/citadel/mark", data=form)
    assert conn.execute("SELECT COUNT(*) FROM citadel_entries").fetchone()[0] == 0
    c.post("/citadel/mark", data={**form, "confirm_password": PASSWORD, "confirm_otp": next_code(secret, conn)})
    total = "SELECT SUM(ranking_delta+spending_delta) FROM points_ledger WHERE discord_id=7 AND kind='award'"
    assert conn.execute(total).fetchone()[0] == 150


def test_transcript_download_is_safe(app, conn, secret, settings):
    """Transcripts download by ticket id; unknown ids 404."""
    from dark_asylum import tickets

    c = app.test_client()
    login(c, secret, conn)
    tid = tickets.create_ticket(conn, 7, "General support", 99)
    msg = {"author_id": 7, "author_name": "g", "created_at": "t", "content": "hello", "attachments": []}
    tickets.archive_ticket(conn, settings.archive_dir, tid, [msg])
    r = c.get(f"/tickets/{tid}/transcript")
    assert r.status_code == 200 and b"hello" in r.data
    assert c.get("/tickets/999/transcript").status_code == 404


def _post_confirmed(c, conn, secret, path, data):
    """POST with a password + OTP confirmation attached.

    The replay guard is rewound so one test can submit several confirmed forms inside the
    same 30-second window (production rejects reuse; see test_otp_cannot_be_replayed).
    """
    conn.execute("UPDATE admins SET last_totp_step=0")
    return c.post(path, data={**data, "confirm_password": PASSWORD, "confirm_otp": next_code(secret, conn)})


def test_survey_post_needs_step_up(app, conn, secret):
    """Surveys are only created after step-up confirmation."""
    c = app.test_client()
    login(c, secret, conn)
    survey = {"csrf": csrf_of(c, "/surveys"), "question": "Q?", "options": "A\nB"}
    c.post("/surveys", data=survey)
    assert conn.execute("SELECT COUNT(*) FROM surveys").fetchone()[0] == 0
    _post_confirmed(c, conn, secret, "/surveys", survey)
    assert conn.execute("SELECT COUNT(*) FROM survey_options").fetchone()[0] == 2


def test_giveaway_post_checks_bank(app, conn, secret):
    """A giveaway is created only when the bank can fund the prize."""
    from dark_asylum import ledger

    c = app.test_client()
    login(c, secret, conn)
    give = {"csrf": csrf_of(c, "/activities"), "title": "P", "prize_gp": "1,000,000", "entry_cost": "10"}
    _post_confirmed(c, conn, secret, "/giveaways", give)  # empty bank -> refused
    assert conn.execute("SELECT COUNT(*) FROM giveaways").fetchone()[0] == 0
    ledger.record_bank(conn, 2_000_000, "seed", ADMIN_A)
    _post_confirmed(c, conn, secret, "/giveaways", give)
    assert conn.execute("SELECT prize_gp FROM giveaways").fetchone()[0] == 1_000_000


def test_event_post_validates_input(app, conn, secret):
    """Bad event input is reported instead of crashing."""
    c = app.test_client()
    login(c, secret, conn)
    ev = {"csrf": csrf_of(c, "/activities"), "title": "Raid", "starts_at": "nope", "recurrence_days": "0"}
    assert _post_confirmed(c, conn, secret, "/events", ev).status_code == 302
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    _post_confirmed(c, conn, secret, "/events", {**ev, "starts_at": "2030-01-01 12:00"})
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_rewards_page_and_step_up(app, conn, secret):
    """The rewards page renders; its POSTs act only with password + OTP confirmation."""
    c = app.test_client()
    login(c, secret, conn)
    assert b"Rewards" in c.get("/rewards").data
    form = {"csrf": csrf_of(c, "/rewards"), "channel": "5", "days": "30"}
    c.post("/rewards/purge/queue", data=form)  # no confirmation -> refused
    assert conn.execute("SELECT COUNT(*) c FROM purge_jobs").fetchone()["c"] == 0
    _post_confirmed(c, conn, secret, "/rewards/purge/queue", form)
    assert conn.execute("SELECT COUNT(*) c FROM purge_jobs").fetchone()["c"] == 1
    _post_confirmed(
        c, conn, secret, "/rewards/item/save", {**form, "name": "Sword", "price": "1,000", "confirmed": "1"}
    )
    assert conn.execute("SELECT price_gp FROM bossing_items").fetchone()["price_gp"] == 1000
    assert _post_confirmed(c, conn, secret, "/rewards/nope/x", form).status_code == 404


def test_health_page_renders_with_heartbeat(app, conn, secret):
    """The health page parses heartbeat timestamps without error."""
    conn.execute("INSERT OR REPLACE INTO heartbeats(name, updated_at) VALUES('bot', ?)", (iso(utcnow()),))
    c = app.test_client()
    login(c, secret, conn)
    assert c.get("/health").status_code == 200


def test_xp_page_and_dxp_requires_step_up(app, conn, secret):
    """The XP page renders, and double-XP dates are only saved after password + MFA confirmation."""
    c = app.test_client()
    login(c, secret, conn)
    assert c.get("/xp").status_code == 200
    form = {"csrf": csrf_of(c, "/xp"), "starts_on": "2030-01-05", "ends_on": "2030-01-07", "note": "Test"}
    c.post("/xp/dxp", data=form)
    assert conn.execute("SELECT COUNT(*) FROM dxp_periods").fetchone()[0] == 0
    _post_confirmed(c, conn, secret, "/xp/dxp", {**form, "ends_on": "2030-01-01"})  # end before start
    assert conn.execute("SELECT COUNT(*) FROM dxp_periods").fetchone()[0] == 0
    _post_confirmed(c, conn, secret, "/xp/dxp", form)
    assert conn.execute("SELECT COUNT(*) FROM dxp_periods").fetchone()[0] == 1


def test_dashboard_members_health_render_with_rs3_data(app, conn, secret):
    """Dashboard cards, rank-promotion view and health text render once RS3 data exists."""
    from dark_asylum import members

    members.link_rsn(conn, 5, "Alpha", 5)
    conn.execute("UPDATE members SET profile_private=1 WHERE discord_id=5")
    c = app.test_client()
    login(c, secret, conn)
    assert "Private profiles" in c.get("/").get_data(as_text=True)
    page = c.get("/members").get_data(as_text=True)
    assert "private profile" in page and "Rank promotion" in page
    assert "every 6 hour" in c.get("/health").get_data(as_text=True) or "6 hour" in c.get("/health").get_data(
        as_text=True
    )


def test_snapshot_interval_setting_validates(app, conn, secret):
    """The snapshot interval is editable within 1-24 hours only."""
    c = app.test_client()
    login(c, secret, conn)
    from dark_asylum.portal.app import EDITABLE_SETTINGS

    assert EDITABLE_SETTINGS["xp_snapshot_hours"]("12") == "12"
    for bad in ("0", "25", "x"):
        try:
            EDITABLE_SETTINGS["xp_snapshot_hours"](bad)
        except ValueError:
            continue
        raise AssertionError(bad)
