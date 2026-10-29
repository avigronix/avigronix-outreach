# Avigronix Outreach Dashboard — Build Plan

Status: **all 8 phases built (22 Oct 2026).** Next: go-live checklist in README on the VPS. Decisions are in section 9.
Sources: `CLAUDE.md` (wins on conflicts), `docs/SRS.md`, `docs/ui/01–04`.

---

## 1. Final v1 feature list

All SRS "Must" items, with the CLAUDE.md simplifications applied. Items marked *(cut)* are not built in v1.

### Auth and users
- [x] Email + password login, argon2 hashing, signed HttpOnly session cookie, CSRF token on every form (FR-1.1, NFR-4)
- [x] Roles: admin, manager, sales; checked on every route (FR-1.2)
- [x] Admin creates / disables / resets users; disabled users are logged out on their next request (FR-1.3)
- [x] Audit log for login, import, campaign start/pause/stop, delete, sender change, suppression change (FR-1.4)
- [x] Login rate limit: 5 failed attempts per minute per IP/email (NFR-4)
- *(cut)* TOTP 2FA (FR-1.5)

### Leads and import
- [x] Upload .xlsx / .csv up to 10 MB / 50,000 rows (FR-2.1)
- [x] 4-step wizard as in UI 03: Upload → Map columns (auto-match, custom fields → JSON) → Validate → Confirm (FR-2.2, 2.3)
- [x] Required source type + list name per import (FR-2.7)
- [x] Validation: syntax, missing email, duplicate in file, duplicate in DB, role address flagged, free-mail flagged, disposable rejected, MX check cached per domain, already suppressed (FR-2.4, 2.5)
- [x] Import report + downloadable CSV of rejected rows with reasons (FR-2.8)
- [x] Timezone: column → US state → country → America/New_York for US → campaign default (FR-2.9)
- [x] Lead list: server-side pagination (50), search, filters (status, tag, source, country, state, campaign), bulk tag, bulk delete, CSV export (FR-2.10)
- [x] Lead detail: fields, notes, timeline of messages, events, replies (FR-2.11)
- [x] Lead statuses as in FR-2.12; tags as `text[]` on `leads`
- *(cut)* third-party verification API (FR-2.6)

### Templates
- [x] Subject (up to 3 A/B variants) + plain-text body, optional simple HTML (FR-3.1, 3.4)
- [x] Merge fields incl. custom fields, fallback `{{first_name|there}}` (FR-3.2)
- [x] Preview against a chosen lead, warning on empty field without fallback (FR-3.3)
- [x] Lint on save: links > 2, images, small spam-word list, size, word count 50–150 (FR-3.5)
- [x] Footer auto-appended (company + postal address + unsubscribe link), cannot be removed (FR-3.6)
- [x] Send test email to an internal address (FR-3.7)
- [x] Rendered subject/body stored on every `messages` row (FR-3.8) — *no template version tables*

### Campaigns and sequences
- [x] Campaign: name, segment (tags + simple filters), provider (SES/SMTP), sender pool, schedule, daily cap (FR-4.1)
- [x] Sequence: step 1 + up to 4 follow-ups, template + delay in business days + "same thread" flag (FR-4.2, 4.3)
- [x] Stop rules: reply, bounce, complaint, unsubscribe, manual stop; optional "stop whole company domain" (FR-4.4)
- [x] States: draft, scheduled, running, paused, completed, stopped; pause effective within 1 minute (FR-4.5)
- [x] Pre-launch checklist must pass before start (FR-4.6)
- [x] A lead can be active in only one campaign (DB partial unique index) (FR-4.7)
- [x] Rules from compliance section: purchased-list leads only via SMTP campaigns; Canada excluded by default

### Scheduling and sending
- [x] Send window in lead's local time, allowed weekdays, US holidays skipped (FR-5.1)
- [x] Caps: min(warm-up cap, sender daily cap, campaign daily cap) + max 3 per recipient domain per day (FR-5.2, 5.5)
- [x] Random gap between sends per sender (SMTP default 60–180 s) (FR-5.3)
- [x] Warm-up schedule per sender, rises only if yesterday was healthy; admin override (FR-5.4)
- [x] Auto-pause sender/campaign at bounce > 3% or complaint > 0.1%, admin alerted (FR-5.6)
- [x] Providers: SES v2 (boto3) and SMTP (smtplib, TLS) behind one `send(msg) -> provider_message_id` (FR-6.1)
- [x] Headers: unique Message-ID, List-Unsubscribe (mailto + https), List-Unsubscribe-Post, Reply-To, In-Reply-To/References on follow-ups (FR-6.2, 6.3, 4.3)
- [x] Retry temporary errors up to 3 with backoff; permanent errors → failed (FR-6.4)
- [x] Idempotent: row inserted before provider call (FR-6.5)
- [x] Suppression check (email + domain) right before every send (FR-6.6)

### Events, replies, unsubscribe
- [x] SNS webhook with signature check; delivery / bounce / complaint / reject events (FR-7.1)
- [x] Hard bounce + complaint → suppression immediately (FR-7.2); 3 soft bounces → hard (FR-7.3)
- [x] SMTP bounces from DSN messages in the sender mailbox via IMAP (FR-7.4)
- [x] IMAP poll every 3 min; match by In-Reply-To/References, fallback sender address (FR-8.1)
- [x] Auto-reply / out-of-office detection, logged only (FR-8.2)
- [x] Real reply stops sequence → lead Replied (FR-8.3)
- [x] Replies screen as in UI 04: list + thread + assign + outcome (FR-8.4)
- [x] Public unsubscribe page via signed token + RFC 8058 one-click POST (FR-9.1–9.3)
- [x] Suppression list: email + domain entries, manual add, CSV import/export (FR-9.4)
- [x] "unsubscribe / remove me" replies flagged for one-click suppression (FR-9.5)
- *(cut)* open/click tracking (FR-7.5)
- [x] Reply from dashboard through the same sender + provider as the original email, BCC reply mailbox (FR-8.5, decision D3)

### Reports and ops
- [x] Dashboard as in UI 01 (FR-10.1)
- [x] Campaign report as in UI 02, per step and per subject variant (FR-10.2)
- [x] Sender health report (FR-10.3)
- [x] CSV export of reports (FR-10.4)
- [x] Alert email on auto-pause and on worker heartbeat stale (NFR-8)
- [x] Nightly `pg_dump` by the worker, keep 7 days on the VPS, upload each dump to S3 (NFR-7, decision D2)
- [x] Delete `events.raw` older than 90 days
- *(cut)* daily summary email (FR-10.5)

---

## 2. Folder structure

```
avigronix-outreach-starter/
├── app/
│   ├── main.py            # FastAPI app, middleware, router includes
│   ├── config.py          # pydantic-settings, reads .env
│   ├── db.py              # engine, SessionLocal, get_db
│   ├── models.py          # all SQLAlchemy models
│   ├── auth.py            # hashing, session user, role check, CSRF
│   ├── seed.py            # python -m app.seed  → first admin + demo data
│   ├── worker.py          # python -m app.worker → the one loop
│   ├── routes/
│   │   ├── auth.py  dashboard.py  leads.py  imports.py  templates.py
│   │   ├── campaigns.py  senders.py  replies.py  suppression.py
│   │   ├── reports.py  users.py  settings.py
│   │   └── public.py      # /u/{token} unsubscribe + /webhooks/ses
│   ├── services/
│   │   ├── importer.py    # read file, map, validate, insert
│   │   ├── validation.py  # email checks, MX cache, role/free/disposable lists, timezone lookup
│   │   ├── render.py      # merge fields, footer, lint, build MIME message + headers
│   │   ├── providers.py   # SesProvider, SmtpProvider (send(msg) -> id)
│   │   ├── scheduler.py   # due enrollments, window, caps, send one, stop rules
│   │   ├── suppression.py # is_suppressed(), suppress()
│   │   ├── events.py      # SNS verify + handle, bounce/complaint, auto-pause
│   │   ├── replies.py     # IMAP poll, match, classify, DSN parse
│   │   ├── warmup.py      # warm-up table + daily step
│   │   └── crypto.py      # Fernet encrypt/decrypt, unsubscribe tokens
│   ├── templates/         # Jinja2: base.html + one folder per area
│   └── static/            # htmx.min.js (vendored), app.css
├── migrations/            # Alembic
├── tests/                 # pytest, core logic only
├── docs/                  # SRS.md, PLAN.md, ui/
├── deploy/                # systemd units: outreach-web, outreach-worker
├── requirements.txt
├── alembic.ini
└── .env.example
```

---

## 3. Database tables

All tables: `id` (bigint PK), `created_at`, `updated_at`. Timestamps stored in UTC.

| Table | Key columns | Notes |
| --- | --- | --- |
| users | email (unique), password_hash, name, role (admin/manager/sales), is_active, last_login_at | |
| audit_logs | user_id, action, entity, entity_id, ip, meta JSONB | Append-only; failed logins also here (used for rate limit) |
| settings | single row: company_name, postal_address, cap_day_tz (America/New_York), bounce_pause_pct (3.0), complaint_pause_pct (0.1), pause_min_sends (50), domain_daily_max (3), worker_heartbeat_at, last_daily_run | Editable on Settings page |
| lead_imports | file_name, stored_path, source_type, list_name, uploaded_by, column_map JSONB, status (uploaded/validating/validated/importing/done/failed), total, ready, duplicates, invalid, suppressed, role_flagged, imported, rejected_csv_path, error | One row per upload; worker does heavy work |
| mx_cache | domain (unique), has_mx, checked_at | Re-check after 30 days |
| leads | email (unique, lower), first_name, last_name, company, domain, title, website, city, state, country, timezone, industry, custom JSONB, tags text[], source_type, import_id, status, owner_id, notes, is_role, is_free_mail, soft_bounce_count | Index: status, domain, import_id, GIN on tags |
| templates | name, subject_variants JSONB (1–3 strings), body_text, body_html (nullable), test_sent_at | |
| senders | type (ses/smtp), from_name, from_email, reply_to, smtp_host, smtp_port, smtp_user, smtp_password_enc, imap_host, imap_port (for DSN bounces), daily_cap, gap_min_sec, gap_max_sec, warmup_day, warmup_override_cap, next_send_at, status (active/paused/disabled), pause_reason, dns_checked_at, dns_ok | Password Fernet-encrypted |
| campaigns | name, provider, segment JSONB (tags + filters), schedule JSONB (windows, weekdays default Mon–Fri, default_tz), daily_cap, stop_company_on_reply, allow_canada, status, pause_reason, started_at, created_by | |
| campaign_senders | campaign_id, sender_id | Unique pair |
| sequence_steps | campaign_id, step_no (1–5), template_id, delay_business_days, same_thread | Unique (campaign_id, step_no) |
| enrollments | campaign_id, lead_id, sender_id (sticky after step 1), current_step, next_send_at, state (active/completed/stopped), stop_reason | Unique (campaign_id, lead_id); **partial unique (lead_id) where state='active'**; index (state, next_send_at) |
| messages | enrollment_id, step_no, sender_id, to_email, to_domain, variant (A/B/C), subject, body_text, body_html, message_id_header, provider_message_id, status (sending/sent/retry/failed/unknown), attempts, retry_at, sent_at, error | **Unique (enrollment_id, step_no)**; index (sender_id, sent_at), (to_domain, sent_at), message_id_header |
| events | message_id, lead_id, type (delivery/bounce_hard/bounce_soft/complaint/reject/reply/auto_reply/unsubscribe), occurred_at, raw JSONB | Index (type, occurred_at); raw cleared after 90 days |
| replies | message_id (nullable), lead_id, mailbox, imap_uid, message_id_header (unique), from_email, subject, body_text, received_at, classification (reply/auto_reply/unsubscribe_request), assigned_to, outcome, handled_at | |
| suppressions | value (email or domain, lower, unique), kind (email/domain), reason (unsubscribe/hard_bounce/complaint/manual/import), source, created_by | Checked before every send |

14 SRS tables + `settings` + `mx_cache`. No tag tables, no template versions, no stats tables (counts come from `messages` / `events` with indexes — fine at 2,000/day).

---

## 4. Pages and routes

All pages are server-rendered; HTMX is used for partial updates (filters, tabs, outcome buttons, import progress polling).

| Route | Page / action | Roles | UI image |
| --- | --- | --- | --- |
| `GET/POST /login`, `POST /logout` | Login | all | — |
| `GET /` | Dashboard: KPI cards, warm-up chart (pure CSS/SVG bars), replies needing action, campaigns, sender health, auto-pause banner | all (sales: own data) | **01** |
| `GET /leads` | Lead list, filters, bulk tag/delete, export | all (sales: own leads, no export) | — |
| `GET /leads/{id}`, `POST /leads/{id}/notes`, `/status` | Lead detail + timeline | all | — |
| `GET /imports` | Import history + reports | admin, manager | — |
| `GET/POST /imports/new` | Step 1 upload | admin, manager | **03** |
| `GET/POST /imports/{id}/map` | Step 2 map columns + source type | admin, manager | **03** |
| `GET /imports/{id}/validate` | Step 3 validation preview (HTMX polls while worker runs) | admin, manager | **03** (right panel) |
| `POST /imports/{id}/confirm`, `GET /imports/{id}/rejected.csv` | Step 4 confirm, rejected CSV | admin, manager | **03** |
| `GET /templates`, `/templates/new`, `/templates/{id}` | List / edit, preview, lint, test send | admin, manager | — |
| `GET /campaigns`, `/campaigns/new`, `/campaigns/{id}/edit` | List / create / sequence builder | admin, manager | — |
| `GET /campaigns/{id}` | Campaign detail: KPIs, steps with variant stats, schedule, stop rules, compliance check | admin, manager | **02** |
| `POST /campaigns/{id}/start\|pause\|resume\|stop` | State changes (start runs preflight) | admin, manager | **02** |
| `GET /replies?tab=needs_action\|interested\|all\|auto` | Replies inbox | all (sales: assigned only) | **04** |
| `GET /replies/{id}`, `POST /replies/{id}/outcome\|assign\|suppress` | Thread + lead card + outcome | all | **04** |
| `POST /replies/{id}/send` | Reply from dashboard (last phase) | all | **04** |
| `GET /senders`, `/senders/{id}`, `POST .../dns-check\|test\|pause\|resume` | Senders, DNS check, warm-up override | admin | — |
| `GET /suppression`, `POST /suppression`, `/suppression/import`, `/suppression/export.csv` | Suppression list | admin, manager | — |
| `GET /reports`, `/reports/campaign/{id}.csv`, `/reports/senders.csv` | Reports + CSV | admin, manager | — |
| `GET /users`, `POST /users`, `/users/{id}/disable\|reset` | User admin | admin | — |
| `GET/POST /settings` | Footer, address, thresholds, audit log view | admin | — |
| `GET /u/{token}`, `POST /u/{token}` | Public unsubscribe page + one-click POST | public | — |
| `POST /webhooks/ses` | SNS events | public (signature-checked) | — |
| `GET /healthz` | Health check for Nginx/monitoring | public | — |

Sidebar matches the mockups: Dashboard, Leads, Imports, Templates, Campaigns, Replies (badge), Senders, Suppression list, Reports, Settings.

---

## 5. How the worker works

One process: `python -m app.worker`. A plain loop:

```
while True:
    write settings.worker_heartbeat_at = now
    run_send_tick()            # every 5 s (see D12)
    if 3 min passed:  poll_imap()
    if import jobs waiting: run_import_jobs()   # validate / insert
    if new day (09:00 IST) and not done today: daily_jobs()
    sleep until next 60 s mark
```

Each job runs in its own try/except, so one failing job logs + alerts but does not kill the loop. systemd (`Restart=always`) restarts the process if it dies.

### 5.1 Send tick (every 5 s)

1. **Retry pass**: messages with `status='retry'` and `retry_at <= now` are retried first (same row, `attempts+1`, max 3, backoff 5 → 15 → 45 min).
2. **Pick due work**: `SELECT … FROM enrollments WHERE state='active' AND next_send_at <= now` joined with running campaigns, `ORDER BY next_send_at LIMIT 500 FOR UPDATE SKIP LOCKED`.
3. For each due enrollment, in order:
   1. **Campaign still running?** (re-read status — this is how pause works within 1 minute.)
   2. **Stop checks**: lead status is replied / bounced / complained / unsubscribed / do-not-contact → stop enrollment with that reason.
   3. **Suppression check**: lead email or its domain in `suppressions` → stop enrollment (`suppressed`).
   4. **Send window**: is "now" inside the window in the lead's timezone, on an allowed weekday, not a US holiday? If not → set `next_send_at` = next window opening, skip.
   5. **Pick sender**: step 1 → the pool sender (active, `next_send_at <= now`) with the most cap left; follow-ups → the same sender as step 1 (thread must stay on one address). None available → skip, try next tick.
   6. **Caps**: today's count for sender < min(warm-up cap, sender daily cap); today's count for campaign < campaign cap; today's count for `to_domain` < 3. "Today" = calendar day in `settings.cap_day_tz` (default America/New_York). Over cap → skip (domain cap: push `next_send_at` to next day's window).
   7. **Render**: pick subject variant (round-robin by enrollment id), merge fields, footer, headers (Message-ID, List-Unsubscribe, List-Unsubscribe-Post, Reply-To, In-Reply-To/References + "Re:" for same-thread follow-ups).
   8. **Insert `messages` row** with `status='sending'` and **commit**. If the unique `(enrollment_id, step_no)` insert fails → already sent, skip.
   9. **Call provider** `send(msg)`.
      - OK → `status='sent'`, `sent_at`, provider id; enrollment moves to next step, `next_send_at` = last send date + N business days, at the window start in the lead's timezone (+ random minutes); last step → `completed`. Lead status → In sequence.
      - Temporary error (throttle, 4xx, connection refused before DATA) → `status='retry'`.
      - Permanent error (5xx, invalid address) → `status='failed'`, enrollment stopped.
      - Crash/timeout where we don't know if it went → row stays `sending`; on startup such rows become `unknown` and are **never** resent automatically (shown on campaign page for a human).
   10. Set `sender.next_send_at = now + random(gap_min, gap_max)`. SMTP: 60–180 s → max 1 send per sender per tick. SES: small gap (default 2–6 s, so ~10–20 per tick) to spread volume over the window instead of blasting at 9:00.
4. After the tick, **auto-pause check** per active sender and running campaign: take the last 200 sends, or all sends in the last 24 h if that is more; only if that window has **at least 50 sends**, and bounce > 3% or complaint > 0.1% → pause + audit log + alert email + dashboard banner.

### 5.2 Warm-up

`senders.warmup_day` (starts at 1). Cap = table lookup by type (SRS 7.3, e.g. SMTP day 1 = 10 … day 28+ = 50; SES day 1 = 50 … 2,000). The daily job increases `warmup_day` by 1 only if yesterday's bounce < 2% and complaint < 0.1%; otherwise it holds. Admin can set `warmup_override_cap`.

### 5.3 IMAP poll (every 3 min)

Mailboxes: the global reply mailbox from `.env` + every active SMTP sender's own mailbox.
1. Fetch unseen messages since the last stored UID (messages stay in Zoho; we don't delete).
2. **DSN bounce?** (`Content-Type: multipart/report; report-type=delivery-status`) → find original by Message-ID in the attachment → hard/soft bounce event → same handling as SES bounces.
3. **Match reply**: `In-Reply-To` / `References` → `messages.message_id_header`; fallback: from address = lead email.
4. **Classify**: auto-reply headers (`Auto-Submitted`, `X-Autoreply`, `X-Autorespond`, `Precedence: auto_reply`) or subjects ("Out of office", "Automatic reply") → `auto_reply` (logged, sequence continues). Subject/body contains "unsubscribe / remove me / stop emailing" → `unsubscribe_request`. Else `reply`.
5. Real reply → save `replies` row, stop enrollment, lead → Replied; if campaign has "stop whole company", stop other active enrollments with the same domain (free-mail domains like gmail.com excluded).

### 5.4 Daily jobs (once per day)

Warm-up step, delete `events.raw` > 90 days, `pg_dump` backup → upload to S3 → delete local dumps > 7 days, refresh stale MX cache, mark campaigns with no active enrollments as completed.

---

## 6. Build phases

Each phase ends with something you can run with `docker compose up -d` and test in the browser. I stop after each phase for your OK.

### Phase 1 — Skeleton, auth, layout ✅ done (22 Oct 2026)
- [x] Dockerfile, docker-compose.yml (db, web, worker), requirements.txt, .env.example
- [x] config.py, db.py, Alembic setup, first migration (users, audit_logs, settings)
- [x] Login/logout, session cookie, CSRF, login rate limit, roles
- [x] base.html + app.css matching mockup sidebar/cards/tables/badges; empty Dashboard page
- [x] Users page (create, disable, reset password), Settings page (footer, address), audit log
- [x] Worker stub: loop + heartbeat only
- [x] `python -m app.seed` creates first admin
- [x] Tests: password hashing, role check
- **You test:** compose up, seed, log in, create a sales user, disable them, see audit log.

### Phase 2 — Leads, import, suppression, unsubscribe ✅ done (22 Oct 2026)
- [x] Tables: lead_imports, mx_cache, leads, suppressions (events table comes with messages in Phase 4)
- [x] Import wizard (UI 03): upload → map (auto-match) → validate in worker → confirm in worker → report + rejected CSV
- [x] Validation rules + MX cache + timezone resolution
- [x] Lead list (filters, pagination, bulk tag/delete, export), lead detail with notes
- [x] Suppression list page: add, CSV import/export
- [x] Public unsubscribe page `/u/{token}` (GET + one-click POST)
- [x] `python -m app.seed --demo` adds demo leads + suppressions
- [x] Tests: email validation, duplicate handling, timezone resolution, suppression check, token sign/verify
- **You test:** import a 5,000-row Excel, check counts + rejected CSV, filter leads, unsubscribe via a token link.

### Phase 3 — Templates, senders, providers ✅ done (22 Oct 2026)
- [x] Tables: templates, senders
- [x] Template editor: variants, merge fields + fallback, preview against a lead, lint warnings
- [x] render.py: final MIME message with footer + all required headers
- [x] providers.py: SES + SMTP; sender page with encrypted SMTP password, DNS check (SPF/DKIM/DMARC; SES identity status via API), "send test"
- [x] Template "send test email" to an internal address
- [x] Tests: merge/fallback, lint, headers present (List-Unsubscribe, Message-ID, Reply-To), Fernet round trip
- **You test:** create a Zoho SMTP sender + SES sender (sandbox), send test emails to yourself, check headers in Gmail "Show original".

### Phase 4 — Campaigns and the sending engine ✅ done (22 Oct 2026)
- [x] Tables: campaigns, campaign_senders, sequence_steps, enrollments, messages
- [x] Campaign create / sequence builder / segment preview / preflight checklist / start-pause-resume-stop
- [x] Campaign detail page (UI 02)
- [x] Worker send tick: window, holidays, caps, domain cap, gaps, warm-up, idempotent send, retries, stop rules, threading
- [x] Tests: send window (timezones, weekends, holidays), business-day delays, caps incl. domain cap, idempotency (crash between insert and send), stop rules, "one active campaign per lead"
- [x] Lead timeline (emails sent) + manual "Stop sequence"; unknown-result sends resolved by a human on the campaign page
- **You test:** 3-step campaign to 3–5 internal addresses in different timezones; watch sends follow the window; restart worker mid-run → no duplicates.

### Phase 5 — SES events, bounces, auto-pause ✅ done (22 Oct 2026)  → *Gate: request SES production access*
- [x] `POST /webhooks/ses`: SNS signature verify, subscription confirm, delivery/bounce/complaint/reject
- [x] Hard bounce/complaint → suppression + stop; soft bounce counter (3 → hard)
- [x] Auto-pause for sender + campaign, banner, alert email; warm-up daily step
- [x] Tests: SNS signature check (valid/invalid), bounce → suppression, auto-pause thresholds
- [x] Delivered / bounced / complaints on the campaign page and lead timeline; auto-pause banner on the dashboard; resume starts a fresh health window
- **You test:** send to SES simulator addresses (bounce@, complaint@simulator.amazonses.com) and watch suppression + auto-pause.

### Phase 6 — IMAP: replies and SMTP bounces ✅ done (22 Oct 2026)
- [x] Tables: replies, imap_cursors (last read UID per mailbox; first poll reads only the last 7 days)
- [x] IMAP poller (reply mailbox + SMTP sender mailboxes), DSN parsing, reply matching, auto-reply + unsubscribe-keyword classification
- [x] Replies inbox (UI 04): tabs, thread view, lead card, assign, outcome, add-to-suppression; company-domain stop
- [x] Tests: matching by headers and fallback, auto-reply detection, DSN parse, company-domain stop (free-mail excluded)
- [x] Sidebar badge with replies needing action; sales see only their own replies
- **You test:** reply to a test email from Gmail → appears within 3 min, sequence stops; send an out-of-office → sequence continues.

### Phase 7 — Dashboard, reports, ops ✅ done (22 Oct 2026) → *Gate: go-live checklist (SRS 11.3, in README)*
- [x] Dashboard (UI 01) with real numbers, warm-up chart, replies needing action, sender health
- [x] Reports: campaign (per step + per variant), sender health, CSV exports
- [x] Alert email on worker heartbeat stale (checked when the dashboard loads, max 1/hour); nightly `pg_dump` + S3 upload (Postgres 16 client in the image), restore tested; worker job failures also alert (max 1/hour per job)
- [x] Full seed demo data so all screens look like the mockups
- [x] README: deploy on VPS, Nginx snippet, backup restore steps
- [x] Lead list campaign filter (rest of FR-2.10); `seed --remove-demo`
- **You test:** run the SRS 11.3 checklist end to end.

### Phase 8 — Reply from dashboard (FR-8.5) ✅ done (22 Oct 2026, waiting for Vikas's OK)
- [x] Reply box on UI 04: send through the same sender + provider that sent the original (SES identity or SMTP mailbox), In-Reply-To/References kept, Reply-To = reply mailbox, BCC reply mailbox; saved on the thread
- [x] Save draft; their message quoted below; footer + unsubscribe headers; refused if the address is suppressed; our copy in the mailbox is ignored by the IMAP poller
- **You test:** reply to a lead from the dashboard; it lands in the same Gmail thread.

---

## 7. `.env` variables

```
# Database
POSTGRES_USER=outreach
POSTGRES_PASSWORD=change-me
POSTGRES_DB=outreach
DATABASE_URL=postgresql+psycopg://outreach:change-me@db:5432/outreach

# App
SECRET_KEY=            # signs session cookie + CSRF + unsubscribe tokens (long random string)
FERNET_KEY=            # encrypts SMTP passwords in DB (Fernet.generate_key())
BASE_URL=https://outreach.avigronix.com   # used in unsubscribe links
DISPLAY_TZ=Asia/Kolkata
UPLOAD_DIR=/data/uploads
BACKUP_DIR=/data/backups

# Amazon SES (keys only here, never in DB or Git)
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
SES_CONFIGURATION_SET=outreach
SNS_TOPIC_ARN=          # only this topic is accepted by the webhook

# Backups to S3 (uses the AWS keys above unless BACKUP_AWS_* are set)
BACKUP_S3_BUCKET=
BACKUP_S3_PREFIX=pg-backups/
BACKUP_AWS_ACCESS_KEY_ID=
BACKUP_AWS_SECRET_ACCESS_KEY=

# Reply mailbox (Zoho) — IMAP for replies, SMTP for alert emails
REPLY_EMAIL=outreach-replies@avigronix.com
REPLY_IMAP_HOST=imap.zoho.com
REPLY_IMAP_PORT=993
REPLY_SMTP_HOST=smtp.zoho.com
REPLY_SMTP_PORT=465
REPLY_PASSWORD=         # Zoho app password

# Alerts
ALERT_EMAIL=vikas@avigronix.com

# Seed (first admin)
SEED_ADMIN_EMAIL=
SEED_ADMIN_PASSWORD=
```

Per-sender SMTP passwords are entered in the Senders page and stored Fernet-encrypted.

---

## 8. Libraries (`requirements.txt`)

fastapi, uvicorn, jinja2, python-multipart, sqlalchemy, psycopg[binary], alembic, pydantic-settings, argon2-cffi, openpyxl, boto3, imap-tools, email-validator, dnspython, holidays, cryptography, itsdangerous, pytest (+ httpx for FastAPI's TestClient in tests). All approved and listed in CLAUDE.md.

Nothing else. Chart on dashboard = plain CSS/SVG, no JS chart library. Timezones via stdlib `zoneinfo`; the server OS provides `tzdata` (no extra Python library).

---

## 9. Decisions (approved 22 Oct 2026)

- **D1. Libraries:** uvicorn, psycopg[binary], python-multipart, argon2-cffi approved (added to CLAUDE.md).
- **D2. Backups:** worker runs `pg_dump` nightly, keeps 7 days on the VPS, uploads each dump to S3 via boto3. Bucket + keys in `.env`. Vikas sets a 30-day lifecycle rule on the bucket.
- **D3. Reply from dashboard:** sent through the SAME sender + provider as the original email (SES campaign → SES identity; SMTP campaign → that mailbox). In-Reply-To/References kept, Reply-To = reply mailbox, BCC the reply mailbox so Zoho has the full thread.
- **D4. Cap day:** America/New_York calendar day, configurable on the Settings page (`settings.cap_day_tz`).
- **D5. Auto-pause:** last 200 sends or last 24 h (whichever is more); evaluated only when the window has at least 50 sends.
- **D6. Weekdays:** Mon–Fri allowed by default, configurable per campaign (simple checkbox list).
- **D7. Holidays:** US holidays skipped for all leads.
- **D8. Soft bounce:** no re-send; count on the lead, sequence continues; 3rd soft bounce → hard bounce + suppression.
- **D9. Company-domain stop:** across all running campaigns; free-mail domains never trigger it.
- **D10. Reply outcomes:** only SRS outcomes (Interested, Not interested, Meeting booked, Wrong person, Unsubscribe request); no "Question" tag.
- **D12. Send tick every 5 s, not 60 s** (Phase 4): SES senders wait 2–6 s between sends; a 60 s tick would cap each sender at ~60/hour. Pause still takes effect within seconds.
- **D13. Send errors have 4 kinds:** temporary (retry, max 3), sender (pause the sender, lead waits), recipient (hard bounce → suppressed), message (failed). A dropped connection mid-send is "unknown" and never resent automatically.
- **D14. Free-mail domains** (gmail.com etc.) are not limited by the 3-per-domain-per-day rule — they are different people, not one company.
- **D15. Auto-pause after resume** (Phase 5): resuming a campaign or sender starts a fresh health window, so old bounces don't pause it again straight away. Clean the list before resuming.
- **D16. Warm-up day moves up** only after a day with sends and bounce < 2%, complaints < 0.1%. Days with no sends (weekends, paused) don't count.
- **D17. Replies** (Phase 6): a reply from anyone that references our Message-ID counts for the lead (e.g. their office manager). "Wrong person" keeps the lead as Replied; "Unsubscribe request" suppresses it. Mail is never moved or deleted in Zoho.
- **D18. Dashboard chart** (Phase 7): bars are real sends per day; the dashed line is *today's* total cap. Past days' caps are not stored (no stats tables).
- **D19. Demo data** only on a test install: `.example` addresses, next sends 60 days away, refuses to run when real campaigns exist; `--remove-demo` deletes it.
- **D20. Dashboard replies** (Phase 8) are stored in `replies` with direction "out" (not in `messages`, which are sequence steps); they never count in inbox numbers or reply rates. Provider interface gained an optional `bcc` (envelope only).
- **D21. Deploy without Docker** (27 Oct 2026, Vikas): Ubuntu 22.04 server runs Python 3.12 (deadsnakes PPA; 3.10 is end-of-life Oct 2026) and PostgreSQL 16 (PGDG apt repo) + `.venv` in `/opt/outreach` + two systemd units in `deploy/`. Dockerfile and docker-compose.yml removed. Tests stay out of Git (`.gitignore`); pytest/httpx are not in `requirements.txt`. `UPLOAD_DIR`/`BACKUP_DIR` default to `data/…` relative to the app folder.
- **D11. Running sequences:** editable only in Draft or Paused; steps already sent cannot be deleted.
- **Still open (needed before Phase 3 testing):** physical postal address for the footer, final sender name/address, reply mailbox owner, lead source.

### Over-engineered or risky in the SRS
- **SES for cold/purchased lists is the biggest business risk.** AWS often denies or later suspends cold outreach. The plan enforces "purchased database → SMTP campaigns only" (as mockup 03 says) and keeps the engine provider-agnostic.
- **SMTP mailboxes (Zoho/Google) for cold mail** may also break their terms at volume; 30–50/day per mailbox means 1,000+/day needs 20+ mailboxes. Realistic early volume is a few hundred/day.
- **NFR-2 "10,000/day"** — the design handles it, but I won't optimize for it (CLAUDE.md caps at 2,000/day).
- **Warm-up "rises only if healthy"** — kept, but as one counter (`warmup_day`), no history table.
- **A/B reply rate per variant** — kept cheaply (variant letter on each message), no stats tables.
- **Spam-word lint** — a short hard-coded word list, warnings only, never blocks saving.
- **"Unknown" sends after a crash** — we never auto-resend; a human decides. Safer than double sending.
- **GDPR "delete on request"** — deleting a lead must keep the email in suppressions, otherwise a future import brings it back. I'll do it that way.
- **Login rate limit** done via failed-login rows in `audit_logs` — no Redis, no extra table.
- **DKIM check for SMTP senders** needs the DKIM selector (e.g. Zoho's); stored per sender.
