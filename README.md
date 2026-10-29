# Avigronix Outreach Dashboard

Internal B2B email outreach tool. Plan and progress: [docs/PLAN.md](docs/PLAN.md). Rules: [CLAUDE.md](CLAUDE.md).

Runs without Docker: PostgreSQL 16 on the server, one Python virtualenv, and two systemd services from the same code —
`outreach-web` (FastAPI on 127.0.0.1:8000, behind Nginx) and `outreach-worker` (sending, IMAP, daily jobs, backups).

## 1. Install on the VPS (Ubuntu 22.04)

Ubuntu 22.04 ships Python 3.10 (end of life Oct 2026) and PostgreSQL 14, so both come from their official extra repos.

```bash
# Python 3.12 (deadsnakes PPA)
sudo apt update
sudo apt install -y software-properties-common curl ca-certificates git
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt install -y python3.12 python3.12-venv

# PostgreSQL 16 from the PostgreSQL apt repo (includes pg_dump 16 for backups)
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt jammy-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list
sudo apt update
sudo apt install -y postgresql-16
# If an older PostgreSQL is already installed, 16 gets port 5433: check with `pg_lsclusters` and use that port in DATABASE_URL.

# Database + user (pick a strong password; it goes into DATABASE_URL)
sudo -u postgres createuser --pwprompt outreach
sudo -u postgres createdb -O outreach outreach

# App user + code in /opt/outreach
sudo useradd --system --home /opt/outreach --shell /usr/sbin/nologin outreach
sudo git clone <repo> /opt/outreach
sudo chown -R outreach:outreach /opt/outreach
cd /opt/outreach
sudo -u outreach python3.12 -m venv .venv
sudo -u outreach .venv/bin/pip install -r requirements.txt

# Config
sudo -u outreach cp .env.example .env
sudo -u outreach nano .env
sudo chmod 600 .env
```

`.env` on the server:

- `DATABASE_URL=postgresql+psycopg://outreach:<password>@localhost:5432/outreach`
- `SECRET_KEY`, `FERNET_KEY` (commands to generate them are in the file), `BASE_URL=https://outreach.avigronix.com`,
  `COOKIE_SECURE=true`, `SEED_ADMIN_EMAIL`, `SEED_ADMIN_PASSWORD`
- For sending: AWS keys + `SES_CONFIGURATION_SET` + `SNS_TOPIC_ARN`, `REPLY_*` (Zoho reply mailbox), `ALERT_EMAIL`
- For backups: `BACKUP_S3_BUCKET` (+ `BACKUP_AWS_*` keys if different from the SES keys)
- `UPLOAD_DIR=data/uploads`, `BACKUP_DIR=data/backups` (relative to `/opt/outreach`)

Never change `FERNET_KEY` after senders are saved (their SMTP passwords would become unreadable).

```bash
# Tables + first admin (seed is safe to run again)
sudo -u outreach .venv/bin/alembic upgrade head
sudo -u outreach .venv/bin/python -m app.seed

# Services (web runs migrations on every start)
sudo cp deploy/outreach-web.service deploy/outreach-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now outreach-web outreach-worker
```

### Nginx (HTTPS in front of the web service)

```nginx
server {
    server_name outreach.avigronix.com;
    client_max_body_size 12m;             # lead uploads are up to 10 MB
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    # listen 443 ssl + certificates via certbot, like your other sites
}
```

### AWS (SES + events)

1. SES (us-east-1): verify the sending domain (Easy DKIM + custom MAIL FROM), DNS records on Cloudflare with the proxy **off** (SRS 6.2).
2. Configuration set named like `SES_CONFIGURATION_SET`, event destination → SNS topic (`SNS_TOPIC_ARN`) for Delivery, Bounce, Complaint, Reject.
3. SNS topic → HTTPS subscription to `https://<your domain>/webhooks/ses` (confirmed automatically).
4. IAM user for sending: only `ses:SendEmail`, `ses:SendRawEmail` (+ `ses:GetEmailIdentity` for the DNS check). Backup user: `s3:PutObject` on the bucket.

## 2. Everyday commands

```bash
systemctl status outreach-web outreach-worker    # both "active (running)"
journalctl -u outreach-worker -f                 # sends, replies, daily jobs
journalctl -u outreach-web -f                    # web errors
sudo systemctl restart outreach-worker           # safe: no email is ever sent twice

# Update to a new version
cd /opt/outreach
sudo -u outreach git pull
sudo -u outreach .venv/bin/pip install -r requirements.txt
sudo systemctl restart outreach-web outreach-worker    # web runs new migrations on start
```

Demo data for a **test install** (never on the live database): `python -m app.seed --demo`, remove with
`python -m app.seed --remove-demo`.

## 3. Backups and restore

Every night (just after midnight New York time) the worker runs `pg_dump`, keeps the file 7 days in
`/opt/outreach/data/backups` and uploads it to `s3://$BACKUP_S3_BUCKET/$BACKUP_S3_PREFIX`. The Settings page shows the
last backup; a failed backup emails ALERT_EMAIL.

**Restore test (do this before go-live, then every few months):**

```bash
ls -lh /opt/outreach/data/backups
sudo -u postgres createdb -O outreach restore_test
sudo -u postgres pg_restore --no-owner --role=outreach -d restore_test /opt/outreach/data/backups/<file>.dump
sudo -u postgres psql -d restore_test -c "select count(*) from leads; select count(*) from messages;"
sudo -u postgres dropdb restore_test
```

From S3 instead: download the file (`aws s3 cp s3://<bucket>/pg-backups/<file>.dump .`), then the same `pg_restore`.
Real disaster: `sudo systemctl stop outreach-web outreach-worker`, restore into the `outreach` database with
`pg_restore --clean --if-exists --no-owner --role=outreach …`, then start both services again.

## 4. Local run on Windows (development)

Uses the portable PostgreSQL in `.localdev/pgsql` (data in `.localdev/pgdata`, port 5433) and the virtualenv in `.venv`.
Run from the project folder in PowerShell.

`.env` differences from the server:

```
DATABASE_URL=postgresql+psycopg://outreach:<password>@localhost:5433/outreach
BASE_URL=http://localhost:8000
COOKIE_SECURE=false
UPLOAD_DIR=.localdev/uploads
BACKUP_DIR=.localdev/backups
```

Start (three terminals; the database only needs starting again after a reboot):

```powershell
.localdev\pgsql\bin\pg_ctl.exe -D .localdev\pgdata -o "-p 5433" -l .localdev\postgres.log start
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000   # http://localhost:8000
.venv\Scripts\python.exe -m app.worker                                                    # sending, replies, daily jobs
```

First time, and after pulling new migrations:

```powershell
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m app.seed
```

Stop the database: `.localdev\pgsql\bin\pg_ctl.exe -D .localdev\pgdata stop`. Restart `uvicorn` after editing `.env`
(`--reload` only watches `.py` files). Starting the worker sends real email if SES/SMTP keys are set and a campaign is
active.

Tests (kept out of Git): `.venv\Scripts\python.exe -m pip install pytest==8.3.4 httpx==0.28.1`, then
`.venv\Scripts\python.exe -m pytest -q` (set `TEST_DATABASE_URL` to a throwaway database for the sending-engine tests).

## 5. Go-live checklist (SRS 11.3)

- [ ] Settings: company name + **postal address** filled in.
- [ ] Senders: DNS check passes; a test email shows SPF, DKIM and DMARC **PASS** in Gmail → Show original.
- [ ] Import a 5,000-row Excel: counts and the rejected-rows CSV look right.
- [ ] 3-step test campaign to internal addresses in different timezones: sends only inside each one's window,
      follow-ups arrive in the same Gmail thread, a reply stops the sequence within ~3 minutes.
- [ ] SES simulator: `bounce@simulator.amazonses.com` and `complaint@simulator.amazonses.com` end up suppressed.
- [ ] One-click unsubscribe from Gmail's "Unsubscribe" link → lead Unsubscribed within a minute.
- [ ] Auto-pause: a test campaign crosses 3% bounces after 50 sends → paused, banner + alert email.
- [ ] `sudo systemctl restart outreach-worker` in the middle of a send window → no duplicate emails.
- [ ] Backup restore test above passes; S3 bucket has the file and a 30-day lifecycle rule.
- [ ] Stop the worker for 6 minutes, open the dashboard → "Worker not running" banner + alert email.
- [ ] SES production access requested (after the bounce/complaint/unsubscribe checks above).
