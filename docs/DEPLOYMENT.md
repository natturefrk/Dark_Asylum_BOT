# Deployment guide

## Topology
Internet → HTTPS reverse proxy (Caddy/nginx, ports 80/443) → portal on `127.0.0.1:8080`. The bot makes outbound
connections only. Never expose the portal port, the SQLite file, `data/` or backup folders directly.

## 1. Service user and files
```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin darkasylum
sudo -u darkasylum git clone <repo> /opt/dark_asylum && cd /opt/dark_asylum
sudo -u darkasylum python3 -m venv .venv && sudo -u darkasylum .venv/bin/pip install -r requirements.txt
sudo -u darkasylum cp .env.example .env && sudo chmod 600 .env
```
Generate `PORTAL_SECRET_KEY` and `FIELD_ENCRYPTION_KEY` with the commands in `.env.example`. Keep `.env` out of git and
back it up separately and securely (losing `FIELD_ENCRYPTION_KEY` forces both admins to re-enrol MFA).

## 2. HTTPS (Caddy example, automatic certificates)
```
portal.example.com {
    reverse_proxy 127.0.0.1:8080
}
```
Keep `PORTAL_SECURE_COOKIES=true` and `PORTAL_TRUSTED_PROXIES=1`. Open only 80/443 in the firewall.
Optionally restrict by IP/VPN at the proxy for extra defence.

## 3. systemd units
`/etc/systemd/system/dark-asylum-portal.service`
```
[Service]
User=darkasylum
WorkingDirectory=/opt/dark_asylum
ExecStart=/opt/dark_asylum/.venv/bin/python -m dark_asylum.cli portal
Restart=always
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/dark_asylum/data /opt/dark_asylum/backups /mnt/second-drive
[Install]
WantedBy=multi-user.target
```
Create `dark-asylum-bot.service` the same way with `... cli bot`. Then `systemctl enable --now` both.

## 4. Backups
The bot runs a backup every 24 h (first run shortly after start) to `BACKUP_DIR` and `BACKUP_SECONDARY_DIR`
(mount a *different physical drive* there), keeps 30 days, and alerts staff on failure. As a safety net, also add a cron
entry that surfaces failures through the exit code/mail: `0 4 * * * cd /opt/dark_asylum && .venv/bin/python -m dark_asylum.cli backup`.
Test restores periodically: stop services, copy a snapshot over `DATABASE_PATH`, start services.

## Outbound access
The bot calls public Jagex endpoints (`secure.runescape.com`, `apps.runescape.com`) over HTTPS; allow that egress.
Intervals are described in the README; set `RS3_API_DISABLED=true` to turn it off.

## 5. Checklist
- [ ] `.env` mode 600, owned by the service user; no secrets in git
- [ ] HTTPS valid; HTTP redirects to HTTPS; HSTS header present
- [ ] Both admins enrolled, backup codes stored offline
- [ ] Bot role placed above roles it manages; staff + command channels set
- [ ] Secondary backup drive mounted and verified on the *System Health* page
- [ ] OS and dependency updates scheduled
