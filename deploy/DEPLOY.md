# Deploy on the shared VPS

This server (Ubuntu 22.04) already runs other live projects. The outreach app is added **next to** them:

| What | Value |
| --- | --- |
| Python | system `python3.10` (3.10.12), own venv in `/opt/outreach/.venv` |
| PostgreSQL | existing PostgreSQL 16: only a new role + database `outreach` |
| Web app | `outreach-web` service on **127.0.0.1:8020**, 2 uvicorn processes |
| Worker | `outreach-worker` service, **exactly 1** process |
| Nginx | one new site file, **port 80 only**, `server_name outreach.avigronix.com` |
| HTTPS | Cloudflare (orange cloud) ends HTTPS; no certificate on the server, no certbot |

> **Do not touch anything else on this server.** Do not edit, disable or reload-test other Nginx site files
> (only `nginx -t` + `reload` after adding ours). Do not change `postgresql.conf`, `pg_hba.conf`, or other
> databases and roles. Do not reinstall or upgrade Python or PostgreSQL. Ports 8000, 8001, 8085, 8100, 9000, 5310,
> 6379, 6380 and 27017 belong to other apps.

Commands the services run:

```
web:    /opt/outreach/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8020 --workers 2 --proxy-headers --forwarded-allow-ips=127.0.0.1
worker: /opt/outreach/.venv/bin/python -m app.worker
```

Never use `--reload` in production. Never start a second worker.

## 0. Before you start (AWS, Cloudflare, Zoho)

- SES (in `AWS_REGION`): sending domain verified with Easy DKIM + custom MAIL FROM; account out of the sandbox.
- SES configuration set `outreach` with an event destination → SNS topic (`SNS_TOPIC_ARN`) for **Delivery, Bounce,
  Complaint, Reject** (other event types are ignored by the app).
- IAM user for sending: `ses:SendEmail`, `ses:SendRawEmail`, `ses:GetEmailIdentity`. IAM user for backups:
  `s3:PutObject` on the backup bucket. S3 bucket with a 30-day lifecycle rule.
- Zoho mailbox `outreach-replies@avigronix.com` with IMAP on and an app password.

### Cloudflare (for `outreach.avigronix.com` only)

1. DNS: `outreach` **A** record → VPS IP, **Proxied (orange cloud)**. The SES DKIM / MAIL FROM / DMARC records stay
   **DNS only (grey cloud)**.
2. SSL/TLS: the server has no certificate, so the mode for this host must be **Flexible** (Cloudflare → server over
   HTTP port 80). If the zone is set to Full or Full (strict) for the other sites, add a **Configuration Rule**
   for hostname `outreach.avigronix.com` → SSL: Flexible, instead of changing the whole zone.
3. SSL/TLS → Edge Certificates: **Always Use HTTPS = On**. Cloudflare then redirects http → https at the edge; the
   app and Nginx never redirect, so there is no redirect loop.
4. Security → WAF → Custom rules: add a rule *"Skip outreach webhooks"*:
   `(http.host eq "outreach.avigronix.com" and (http.request.uri.path eq "/webhooks/ses" or starts_with(http.request.uri.path, "/u/")))`
   → action **Skip** (all managed rules, Bot Fight Mode / Super Bot Fight Mode, rate limiting).
   Without it, SNS notifications and one-click unsubscribes from Gmail/Yahoo can be challenged and lost.

Behind Cloudflare, `BASE_URL=https://outreach.avigronix.com` and `COOKIE_SECURE=true` are correct: unsubscribe links
are built from `BASE_URL` (https), the session cookie is `Secure` (browsers only talk https to Cloudflare), Nginx
always passes `X-Forwarded-Proto: https`, and the app only issues relative redirects.

## 1. Packages (only if missing)

```bash
python3 --version                      # Python 3.10.12
sudo apt install -y python3.10-venv    # only if "python3 -m venv" says ensurepip is missing
psql --version                         # 16.x, already installed: do NOT reinstall
```

## 2. Database (new role + database only)

```bash
sudo -u postgres createuser --pwprompt outreach     # strong password, goes into DATABASE_URL
sudo -u postgres createdb -O outreach outreach
sudo -u postgres psql -p 5432 -c '\l outreach'      # check it exists; use the port your PostgreSQL 16 runs on
```

The app connects over TCP to `localhost` with a password. If the existing `pg_hba.conf` already allows
`host all all 127.0.0.1/32 scram-sha-256` (the Ubuntu default), nothing else is needed. If it does not, stop and ask
whoever owns the database server; do not edit it yourself.

## 3. Code and virtualenv

```bash
sudo useradd --system --home /opt/outreach --shell /usr/sbin/nologin outreach
sudo git clone -b main https://github.com/avigronix/avigronix-outreach.git /opt/outreach
sudo chown -R outreach:outreach /opt/outreach
cd /opt/outreach
sudo -u outreach python3.10 -m venv .venv
sudo -u outreach .venv/bin/pip install --upgrade pip
sudo -u outreach .venv/bin/pip install -r requirements.txt
sudo -u outreach mkdir -p data/uploads data/backups
sudo chmod 750 /opt/outreach/data
```

## 4. Configuration

```bash
sudo -u outreach cp deploy/.env.production.example .env
sudo chmod 600 .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"                               # → SECRET_KEY
python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"     # → FERNET_KEY
sudo -u outreach nano .env      # replace every CHANGE_ME
```

Use new keys here, never ones from a laptop or a chat. Keep a copy of `FERNET_KEY` in a password manager: if it is
lost, saved SMTP passwords can't be read and have to be entered again. If PostgreSQL 16 is not on port 5432, put its
port in `DATABASE_URL`.

## 5. Tables and first admin

```bash
cd /opt/outreach
sudo -u outreach .venv/bin/python -m alembic upgrade head      # 0001 … 0009
sudo -u outreach .venv/bin/python -m app.seed
sudo -u outreach sed -i 's/^SEED_ADMIN_PASSWORD=.*/SEED_ADMIN_PASSWORD=/' .env    # not needed any more
```

Do not run `python -m app.seed --demo` on this database.

## 6. Services

```bash
sudo ss -ltnp | grep ':8020' || echo "8020 is free"
sudo cp deploy/outreach-web.service deploy/outreach-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now outreach-web outreach-worker
systemctl status outreach-web outreach-worker          # both "active (running)"
curl -s http://127.0.0.1:8020/healthz                  # {"ok":true}
```

## 7. Nginx (one new site, port 80)

```bash
sudo cp deploy/nginx-outreach.conf /etc/nginx/sites-available/outreach
sudo ln -s /etc/nginx/sites-available/outreach /etc/nginx/sites-enabled/outreach
sudo nginx -t                  # must say "test is successful" before the reload
sudo systemctl reload nginx    # reload, not restart: the other sites keep running
curl -s -H 'Host: outreach.avigronix.com' http://127.0.0.1/healthz     # {"ok":true}
```

If `nginx -t` fails, remove the symlink again (`sudo rm /etc/nginx/sites-enabled/outreach`) and fix our file; never
edit another site to make it pass. Then open https://outreach.avigronix.com and log in.

## 8. SNS → webhook

In SNS, on the topic from `SNS_TOPIC_ARN`: Create subscription → HTTPS → `https://outreach.avigronix.com/webhooks/ses`.
The app confirms it by itself. Check:

```bash
journalctl -u outreach-web | grep "SNS subscription confirmed"
```

The subscription must show **Confirmed** in the SNS console. A "Rejected SNS message" log line means the topic ARN in
`.env` does not match. Nothing in the logs at all usually means Cloudflare blocked it: check the WAF skip rule.

## 9. In the app (admin)

1. Settings: company name and **postal address** (campaigns can't start without it).
2. Senders → new SES sender: from address on the verified domain, Reply-To = `outreach-replies@avigronix.com`.
   Warm-up starts at 50/day and rises only after healthy days; the gap between sends is at least 1 s. Run the DNS
   check; send a test to a Gmail address and check SPF, DKIM and DMARC all say PASS under "Show original".
3. Then go through the go-live checklist in README section 5 (SES simulator bounce/complaint, unsubscribe, restart
   during a send window, restore test).

## 10. Backups

The worker backs up every night: `pg_dump` → `/opt/outreach/data/backups` (7 days) → S3. The Settings page shows the
last one, and a failure emails `ALERT_EMAIL`. `pg_dump` must be version 16 (`pg_dump --version`), which it is when
PostgreSQL 16 is the installed server. Backup now (before an upgrade, for example):

```bash
sudo -u outreach /opt/outreach/deploy/backup.sh
```

Optional extra daily copy from cron (13:30 server time), in `/etc/cron.d/outreach-backup`:

```
30 13 * * * outreach /opt/outreach/deploy/backup.sh >> /opt/outreach/data/backup.log 2>&1
```

Restore steps: README section 3. Do the restore test once before go-live, into a scratch database
(`restore_test`), never into another app's database.

## 11. Everyday

```bash
journalctl -u outreach-worker -f          # sends, replies, daily jobs
journalctl -u outreach-web -f             # web errors
sudo systemctl restart outreach-worker    # safe: no email is sent twice

# Update to a new version
cd /opt/outreach
sudo -u outreach /opt/outreach/deploy/backup.sh
sudo -u outreach git pull
sudo -u outreach .venv/bin/pip install -r requirements.txt
sudo systemctl restart outreach-web outreach-worker    # web runs new migrations on start
```

After changing `.env`, restart both services.
