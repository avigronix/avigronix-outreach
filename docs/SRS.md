# Avigronix Outreach Dashboard — Software Requirements Specification

**Avigronix Technologies** · Version 1.0 · 22 Oct 2026

> Note for the developer: section 3 lists the stack originally proposed. The simplified stack in `CLAUDE.md` (project root) overrides it. UI mockups are in `docs/ui/`.

## 1. Introduction

Avigronix will build an in-house B2B email outreach dashboard on its existing VPS (FastAPI + PostgreSQL) to send personalized cold and follow-up emails to 1,000–2,000 business leads per day, about 70–80% of them in the US.

**Purpose.** This SRS defines what the system must do, how it is built, and the compliance rules it must enforce, so it can be implemented and tested phase by phase.

**Scope — in:**

- Lead import from Excel/CSV, validation, de-duplication and segmentation
- Reusable email templates with merge fields
- Campaigns with multi-step follow-up sequences and timezone-aware scheduling
- Sending through pluggable providers: Amazon SES (API) and SMTP mailboxes (Zoho / Google Workspace)
- Tracking of delivery, bounce, complaint, unsubscribe, reply (and optional open/click)
- Suppression list, unsubscribe page, warm-up throttling and reports

**Scope — out (v1):** CRM pipeline/deal stages, LinkedIn automation, AI-written emails, lead scraping or buying, multi-tenant use by other companies.

**Definitions**

| Term | Meaning |
| --- | --- |
| Lead | One contact (person + company) imported into the system |
| Campaign | A named outreach run: a lead segment + a sequence + a sender pool |
| Sequence | Ordered steps (initial email + follow-ups) with delays between them |
| Sender | A sending identity: an SES verified address or an SMTP mailbox |
| Suppression list | Emails that must never be mailed again (unsubscribed, bounced, complained) |
| Warm-up | Gradual daily volume increase for a new domain or mailbox |
| SES | Amazon Simple Email Service |

**Conventions.** Requirements carry IDs (FR-x.y, NFR-x). "Must" = mandatory for v1; "Should" = planned, can slip to v1.1.

## 2. Overall description

The dashboard is a single-company internal tool used by the Avigronix sales team; it replaces manual mail-merge and gives one place to see who was contacted, who replied and who must not be contacted again.

**User roles**

| Role | Who | Can do |
| --- | --- | --- |
| Admin | Vikas (owner) | Everything: senders, DNS checks, users, provider keys, global limits, delete data |
| Manager | Avinash / team lead | Import leads, build templates and campaigns, start/pause campaigns, all reports |
| Sales executive | Sales team members | View assigned leads and replies, mark reply outcome, add notes; cannot send bulk |

**Assumptions**

- Leads are business contacts (work email addresses), mostly US-based; the remaining 20–30% may include UK/EU/other.
- Lead source is not yet confirmed (opt-in vs purchased/compiled list). The design therefore supports both SES and SMTP mailbox sending, switchable per campaign.
- Team mail stays on Zoho Mail; outreach uses a separate subdomain or secondary domain so the main domain reputation is protected.
- DNS for the domain is managed on Cloudflare.

**Constraints**

- Runs on the existing VPS; no new paid infrastructure except the sending provider.
- Amazon SES starts in sandbox (200 emails/day, verified recipients only) until AWS approves production access; approval for cold outreach is not guaranteed.
- Daily volume is capped by warm-up stage and provider quota, not by the target of 1,000–2,000/day.
- Inbox placement cannot be guaranteed; the system can only maximize it through authentication, list hygiene and throttling.

## 3. System architecture and tech stack (original proposal)

Everything runs on the existing VPS; only the sending provider (SES or SMTP mailboxes) is external, and it is swappable per campaign.

Components: Nginx (HTTPS) → web app (UI, API, webhooks) → PostgreSQL. A scheduler/send worker picks due enrollments, applies caps and windows, sends through SES or SMTP, and records results. SES reports delivery, bounce and complaint events back over SNS to a webhook. Replies reach the Zoho reply mailbox and are matched by an IMAP poller.

| Layer | Original choice |
| --- | --- |
| Backend | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 + Alembic |
| Database | PostgreSQL 16 |
| Jobs and scheduler | Redis + ARQ |
| Frontend | React + Vite + Tailwind |
| Excel / CSV | pandas + openpyxl |
| Email libraries | boto3 (SES v2), aiosmtplib, imap-tools, email-validator, dnspython |
| Deployment | Docker Compose, Nginx, Let's Encrypt |
| Monitoring | JSON logs, Telegram/email alerts |

## 4. Functional requirements

Every requirement below is "Must" unless marked "Should".

### 4.1 Authentication and users

- **FR-1.1** Email + password login with secure password hashing; session kept in an HttpOnly cookie.
- **FR-1.2** Role-based access: Admin, Manager, Sales executive (section 2).
- **FR-1.3** Admin can create, disable and reset users. Disabled users lose access immediately.
- **FR-1.4** Every sensitive action (send start, import, delete, sender change) is written to an audit log with user, time and IP.
- **FR-1.5** *Should:* TOTP two-factor login for Admin.

### 4.2 Lead import and management

- **FR-2.1** Upload .xlsx or .csv up to 10 MB / 50,000 rows.
- **FR-2.2** Column-mapping screen: map file columns to system fields (email, first name, last name, company, title, website, city, state, country, timezone, industry, plus any custom fields stored as JSON).
- **FR-2.3** Preview the first 20 rows before confirming import.
- **FR-2.4** Validation on import: email syntax, missing email, duplicate within file, duplicate already in database, role addresses (info@, sales@, admin@) flagged, free-mail domains (gmail, yahoo, outlook) flagged, disposable domains rejected.
- **FR-2.5** MX record check per domain (cached per domain).
- **FR-2.6** *Should:* optional third-party verification API (ZeroBounce / NeverBounce) with status: valid, invalid, catch-all, unknown.
- **FR-2.7** Every lead records its **source** (file name, list name, and a required "source type": opt-in, existing client, inquiry, event, purchased database, manual research).
- **FR-2.8** Import report: total, imported, duplicates, invalid, suppressed, with a downloadable CSV of rejected rows and reasons.
- **FR-2.9** Timezone resolution: use the timezone column if present; otherwise derive from US state / country; fallback America/New_York for US, else the campaign default.
- **FR-2.10** Lead list with search, filter (status, tag, source, country, state, campaign), bulk tag, bulk delete, export CSV.
- **FR-2.11** Lead detail page: fields, notes, full timeline of every email sent, event and reply.
- **FR-2.12** Lead statuses: New, In sequence, Replied, Interested, Not interested, Bounced, Unsubscribed, Complained, Do not contact.

### 4.3 Templates

- **FR-3.1** Create/edit templates with subject + body; plain-text first, simple HTML optional.
- **FR-3.2** Merge fields `{{first_name}}`, `{{company}}`, `{{title}}`, `{{city}}`, any custom field, with fallback syntax `{{first_name|there}}`.
- **FR-3.3** Live preview against a chosen lead; warning if any merge field resolves empty with no fallback.
- **FR-3.4** Subject-line A/B variants (up to 3) per step; system rotates them evenly and reports reply rate per variant.
- **FR-3.5** Lint check before saving: number of links (warn if more than 2), image count, spam-trigger words, total size.
- **FR-3.6** Footer block is mandatory and auto-appended: company name, physical postal address, unsubscribe link.
- **FR-3.7** Send test email to an internal address.
- **FR-3.8** Sent emails keep the exact subject/body that was sent, even if the template is edited later.

### 4.4 Campaigns and sequences

- **FR-4.1** Create a campaign: name, lead segment (tag or filter), sender pool, sending provider (SES or SMTP), sequence, schedule window, daily cap.
- **FR-4.2** Sequence builder: step 1 (initial) + up to 4 follow-ups; each step has its template and a delay in business days (e.g. +3, +5, +7).
- **FR-4.3** Follow-ups are sent as replies in the same thread (`In-Reply-To` and `References` headers, "Re:" subject) unless the step is set to "new thread".
- **FR-4.4** Stop rules: a lead leaves the sequence automatically on reply, bounce, complaint, unsubscribe or manual stop. Reply from anyone at the same company domain can optionally stop all leads of that domain.
- **FR-4.5** Campaign states: Draft, Scheduled, Running, Paused, Completed, Stopped. Pause takes effect within 1 minute.
- **FR-4.6** Pre-launch checklist must pass: sender verified, footer present, test email sent, segment has sendable leads, no suppressed leads included.
- **FR-4.7** A lead cannot be active in two running campaigns at once.

### 4.5 Scheduling and throttling

- **FR-5.1** Send window per campaign in the **recipient's local time** (default Tue–Thu 9:00–11:30 and 13:30–16:30, Mon–Fri allowed), skipping weekends and US public holidays.
- **FR-5.2** Per-sender daily cap and per-campaign daily cap; the lower one wins.
- **FR-5.3** Random gap between sends from the same sender (default 60–180 seconds for SMTP mailboxes; SES limited by account send rate).
- **FR-5.4** Warm-up schedule per sender: day-by-day cap that rises automatically (section 7.3); cannot be exceeded without Admin override.
- **FR-5.5** Domain spread: no more than N emails (default 3) to the same recipient company domain per day.
- **FR-5.6** Auto-pause guard: pause the sender/campaign if, over the last 24 h or 200 sends, bounce rate > 3% or complaint rate > 0.1%; Admin is notified.

### 4.6 Sending engine

- **FR-6.1** Provider interface with two implementations: SES (SES v2 API via boto3) and SMTP (TLS, e.g. Zoho/Google Workspace mailbox).
- **FR-6.2** Each email gets a unique `Message-ID`, `List-Unsubscribe` (mailto + https) and `List-Unsubscribe-Post: List-Unsubscribe=One-Click` headers.
- **FR-6.3** Reply-To is set to a monitored mailbox (section 6).
- **FR-6.4** Retries on temporary errors (throttling, 4xx) with backoff, maximum 3; permanent errors mark the message failed.
- **FR-6.5** Idempotency: a sequence step is sent at most once per lead even if the worker crashes and restarts.
- **FR-6.6** Suppression list is checked at send time, not only at import.

### 4.7 Tracking and events

- **FR-7.1** SES events (Send, Delivery, Bounce, Complaint, Reject) received via SNS → HTTPS webhook; SNS signature verified.
- **FR-7.2** Hard bounce and complaint add the address to the suppression list immediately.
- **FR-7.3** Soft bounce: retry later; after 3 soft bounces treat as hard.
- **FR-7.4** For SMTP sending, bounces are detected by parsing DSN messages arriving in the sender mailbox (IMAP).
- **FR-7.5** *Should:* Open tracking (pixel) and click tracking (redirect), off by default.

### 4.8 Reply detection and inbox

- **FR-8.1** IMAP polling (every 2–5 minutes) of the reply mailbox; replies matched to the sent email by `In-Reply-To`/`References`, fallback by sender address.
- **FR-8.2** Auto-replies and out-of-office messages are detected (headers `Auto-Submitted`, `X-Autoreply`, common subjects) and do not stop the sequence; they are logged.
- **FR-8.3** A real reply stops the sequence and moves the lead to Replied.
- **FR-8.4** Unified replies screen: thread view, assign to a sales executive, mark outcome (Interested, Not interested, Meeting booked, Wrong person, Unsubscribe request).
- **FR-8.5** *Should:* reply from the dashboard through the same mailbox so the thread stays intact.

### 4.9 Unsubscribe and suppression

- **FR-9.1** Public unsubscribe page (no login) reached through a signed token; one click confirms.
- **FR-9.2** One-click POST endpoint for RFC 8058 `List-Unsubscribe-Post` requests.
- **FR-9.3** Unsubscribes take effect immediately.
- **FR-9.4** Global suppression list: email and whole-domain entries; manual add, CSV import, export.
- **FR-9.5** "Unsubscribe" or "remove me" replies flagged by keyword for one-click suppression.

### 4.10 Reports and dashboard

- **FR-10.1** Home dashboard: today's sent / scheduled / remaining cap, 7-day bounce and complaint rates with threshold colours, replies awaiting action.
- **FR-10.2** Campaign report: sent, delivered, bounced, complaints, unsubscribes, replies, positive replies, per step and per subject variant, with rates.
- **FR-10.3** Sender health report: daily volume vs warm-up cap, bounce and complaint trend per sender.
- **FR-10.4** Export reports as CSV.
- **FR-10.5** *Should:* daily summary email to Admin at 9:00 IST.

## 5. Data model (PostgreSQL)

All tables have `id`, `created_at`, `updated_at`; migrations with Alembic.

| Table | Key fields | Notes |
| --- | --- | --- |
| users | email (unique), password_hash, role, is_active, last_login_at | Roles: admin, manager, sales |
| audit_logs | user_id, action, entity, entity_id, ip, meta JSONB | Append-only |
| lead_imports | file_name, source_type, list_name, uploaded_by, total, imported, rejected, status | One row per upload |
| leads | email (unique, lower-case), first_name, last_name, company, domain, title, city, state, country, timezone, industry, custom JSONB, tags, source_type, import_id, status, owner_id, notes | Index on status, domain, import_id |
| templates | name, subject_variants JSONB, body_text, body_html | |
| senders | type (ses / smtp), from_name, from_email, reply_to, smtp_host, smtp_port, smtp_user, encrypted password, daily_cap, warmup_start_date, status | Secrets encrypted |
| campaigns | name, provider, segment (tag/filter), schedule JSONB, daily_cap, status, started_at | |
| campaign_senders | campaign_id, sender_id | Rotation pool |
| sequence_steps | campaign_id, step_no, template_id, delay_business_days, same_thread | Max 5 steps |
| enrollments | campaign_id, lead_id, current_step, next_send_at, state, stop_reason | Unique (campaign_id, lead_id); index (state, next_send_at) |
| messages | enrollment_id, step_no, sender_id, provider_message_id, message_id_header, subject, body, variant, status, sent_at, error | Unique (enrollment_id, step_no) = idempotency |
| events | message_id, type (delivery, bounce_hard, bounce_soft, complaint, reply, auto_reply, unsubscribe), occurred_at, raw JSONB | Index (type, occurred_at) |
| replies | message_id, lead_id, from_email, subject, body_text, received_at, classification, assigned_to, outcome | |
| suppressions | value (email or domain), kind, reason, source, created_by | Unique (value); checked before every send |

**Retention.** Raw provider payloads in `events.raw` deleted after 90 days.

## 6. Sending infrastructure, DNS and replies

Outreach mail is sent from an isolated identity so that reputation damage stays away from avigronix.com and the team's Zoho mail; replies always land in a mailbox a human reads.

### 6.1 Sending options

| | Option A: Amazon SES | Option B: SMTP mailboxes |
| --- | --- | --- |
| Use when | Leads are opt-in, existing clients, inquiries | Leads are compiled or purchased B2B lists |
| Identity | Subdomain, e.g. mail.avigronix.com | Secondary domain, e.g. avigronix.in |
| Senders | 1–2 addresses on the subdomain | 3–5 named mailboxes on Zoho or Google Workspace |
| Volume after warm-up | 1,000–2,000/day | 30–50/day per mailbox |
| Region | us-east-1 | n/a |
| Approval | AWS production access required | None, but provider terms apply |

Recommended sender: a person ("Vikas from Avigronix", vikas@mail.avigronix.com).

### 6.2 DNS records on Cloudflare (Option A)

Records go on the subdomain only; existing Zoho MX, SPF and DKIM records on avigronix.com are not touched.

| Record | Host | Value (example) |
| --- | --- | --- |
| DKIM (Easy DKIM) | 3 CNAMEs `xxxx._domainkey.mail` | Values given by SES |
| MAIL FROM MX | bounce.mail | `10 feedback-smtp.us-east-1.amazonses.com` |
| MAIL FROM SPF | bounce.mail | `v=spf1 include:amazonses.com ~all` |
| DMARC | _dmarc.mail | `v=DMARC1; p=none; rua=mailto:dmarc@avigronix.com` |

Cloudflare proxy must be off for all mail records.

### 6.3 Replies

- Reply-To on every email = a monitored mailbox on Zoho (e.g. outreach-replies@avigronix.com, or the sender's own mailbox in Option B).
- The dashboard reads that mailbox over IMAP (app password) for reply detection; messages stay in Zoho.

### 6.4 SES setup checklist

1. AWS account with company email, MFA on.
2. Verify the domain in us-east-1, Easy DKIM, custom MAIL FROM.
3. Configuration set with SNS event destination → `https://<vps>/webhooks/ses`.
4. Build unsubscribe, suppression and bounce handling first; test in sandbox.
5. Request production access (use case, list source, opt-out, bounce handling, sample email, starting volume ~500/day).
6. IAM user limited to `ses:SendEmail` / `ses:SendRawEmail`.

## 7. Compliance and deliverability rules

The system enforces these rules in code, so a user cannot launch a campaign that breaks them.

### 7.1 Legal (US-first)

| Law / rule | Applies to | What the system enforces |
| --- | --- | --- |
| CAN-SPAM Act (US) | US recipients | Truthful From/subject, physical postal address in footer, working opt-out on every email, opt-out honoured immediately, no sending to suppressed addresses |
| GDPR / UK PECR | EU and UK recipients | Lead source recorded, opt-out on every email, data deletion on request |
| CASL (Canada) | Canadian recipients | Canada excluded from cold campaigns by default |
| India DPDP Act | Data processed by Avigronix | Access control, encryption of secrets, deletion on request |

### 7.2 Mailbox-provider sender rules (Gmail, Yahoo, Microsoft)

- SPF and DKIM pass, DMARC published, From domain aligned.
- One-click unsubscribe headers on every email.
- Spam complaint rate below 0.3%; system pause threshold is 0.1%.

### 7.3 Warm-up plan (per new sender)

| Week | SES identity: emails/day | SMTP mailbox: emails/day |
| --- | --- | --- |
| 1 | 50 → 100 | 10 → 15 |
| 2 | 150 → 300 | 20 → 25 |
| 3 | 400 → 700 | 30 → 40 |
| 4 | 800 → 1,200 | 40 → 50 |
| 5+ | up to 2,000 if bounce < 2% and complaints < 0.1% | 50 max |

The cap rises only if the previous day's metrics are inside the limits; otherwise it holds.

### 7.4 Content rules

- Plain-text style, 50–150 words, one call to action, at most 1–2 links, no attachments, no link shorteners.
- Follow-ups shorter than step 1; maximum 3–4 follow-ups.

### 7.5 Health thresholds

| Metric | Healthy | Warning | Auto-pause |
| --- | --- | --- | --- |
| Bounce rate | < 2% | 2–3% | > 3% |
| Complaint rate | < 0.05% | 0.05–0.1% | > 0.1% |
| Unsubscribe rate | < 1% | 1–2% | Review copy and list |

## 8. Non-functional requirements

| ID | Area | Requirement |
| --- | --- | --- |
| NFR-1 | Performance | Pages load in < 2 s; lead list paginated server-side (50 rows); 50,000-row import runs in the background |
| NFR-2 | Throughput | Sending engine handles 10,000 emails/day; scheduler tick every 60 s |
| NFR-3 | Reliability | No duplicate sends after a crash; services restart automatically |
| NFR-4 | Security | HTTPS only; CSRF protection on forms; login rate limit (5/min); provider keys and SMTP passwords encrypted (Fernet), key in env, never in Git |
| NFR-5 | Webhook security | SNS messages verified by signature; unsubscribe tokens signed (HMAC) |
| NFR-6 | Privacy | Lead data only for logged-in roles; export restricted to Admin and Manager |
| NFR-7 | Backup | Nightly `pg_dump` kept 14 days; restore tested before go-live |
| NFR-8 | Observability | Clear logs; alert email on worker failure and auto-pause |
| NFR-9 | Usability | Desktop Chrome/Edge; usable on phone for replies and dashboard |
| NFR-10 | Maintainability | New provider can be added without changing campaign logic; tests for scheduler, suppression and stop rules |

## 9. Routes summary (original API proposal)

| Group | Purpose |
| --- | --- |
| Auth | login, logout |
| Users | admin user management |
| Imports | upload, mapping, confirm, report |
| Leads | list, detail, bulk tag/delete/status, export |
| Templates | CRUD, preview, test-send, lint |
| Senders | CRUD, DNS check, test, warm-up |
| Campaigns | CRUD, preflight, start, pause, resume, stop |
| Replies | list, assign, outcome, respond |
| Suppression | list, add, delete, import |
| Reports | overview, campaign, sender, CSV export |
| Public | `GET/POST /u/{token}` unsubscribe |
| Webhooks | `POST /webhooks/ses` |

## 10. Implementation phases

| Phase | Weeks | Content |
| --- | --- | --- |
| 0 Setup | 1 | Domain/subdomain, DNS, AWS SES sandbox, Zoho reply mailbox, project skeleton, deploy |
| 1 Core | 1–3 | Auth and roles, Excel import with mapping and validation, leads, templates, senders, suppression, unsubscribe page |
| 2 Sending engine | 3–5 | Campaigns and sequences, scheduler with caps/windows/warm-up, SES + SMTP providers, SNS webhook, bounce/complaint handling, auto-pause |
| Gate | | Request SES production access once unsubscribe and bounce handling work |
| 3 Replies and reports | 5–6 | IMAP poller, reply inbox, dashboard, reports, CSV export, alerts, backups |
| Gate | | Go-live checklist (11.3) passes |
| 4 Pilot | 7–10 | Start 50/day to best leads, raise per warm-up plan while metrics stay healthy |

## 11. Risks, open questions and acceptance

### 11.1 Risks

| Risk | Mitigation |
| --- | --- |
| SES production access denied | Provider-agnostic engine; switch to SMTP mailboxes |
| SES account under review after launch | Auto-pause thresholds; verified lists; warm-up |
| Poor lead list | MX check, role/free-mail filters, source tracking |
| Spam folder | Authentication, plain text, few links, personalization, warm-up |
| Domain blacklisted | Separate subdomain / secondary domain |
| Legal complaint | Mandatory footer, unsubscribe, suppression, source records, Canada gating |
| VPS failure | Idempotent sending, nightly backups, restart policies |

### 11.2 Open questions

- [ ] Lead source (opt-in, existing contacts, purchased database, manual research)?
- [ ] Physical postal address for the footer?
- [ ] Final sender name and address?
- [ ] Reply mailbox and who handles replies?
- [ ] First offers to pitch (for templates)?

### 11.3 Acceptance criteria (go-live)

- [ ] Import of a 5,000-row Excel file maps, validates and reports correctly.
- [ ] Test campaign of 3 steps sends to internal addresses in their local-time window, threads follow-ups, and stops on reply.
- [ ] Hard bounce and complaint (SES simulator addresses) suppress the lead automatically.
- [ ] One-click unsubscribe from Gmail's UI removes the lead within 1 minute.
- [ ] Auto-pause fires when thresholds are crossed in a test.
- [ ] SPF, DKIM, DMARC show pass in Gmail "Show original".
- [ ] Restarting the worker mid-campaign causes no duplicate emails.
- [ ] Backup restore tested on a separate database.
