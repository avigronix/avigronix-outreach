# Avigronix Outreach Dashboard — project rules

Internal B2B email outreach tool for Avigronix Technologies. Requirements: `docs/SRS.md`. UI mockups: `docs/ui/*.png` (01 dashboard, 02 campaign detail, 03 lead import, 04 replies inbox). Plan and progress: `docs/PLAN.md`.

## Goal

A complete, working product with the smallest setup that does the job: one codebase, one database, no build step, deployable on the existing VPS without Docker (PostgreSQL + venv + two systemd services). Used by 3–10 internal users and 1,000–2,000 emails/day. Do not design for more than that.

## Stack (this overrides SRS section 3)

| Need | Use | Do NOT add |
| --- | --- | --- |
| Web app | FastAPI, sync endpoints | async everywhere, microservices |
| UI | Server-rendered Jinja2 templates + HTMX (vendored `static/htmx.min.js`) + one hand-written `static/app.css` matching `docs/ui` | React/Vite/Next, npm build, Tailwind build, UI kits |
| Database | PostgreSQL 16, SQLAlchemy 2 (sync) + Alembic | Redis, MongoDB, repositories/unit-of-work layers |
| Auth | Session cookie (signed, HttpOnly) + argon2/bcrypt hashing + CSRF token on forms | JWT access/refresh tokens, OAuth |
| Background work | ONE worker process `python -m app.worker`: a loop that runs the send tick every 5 s (agreed in Phase 4, so SES can pace sends 2–6 s apart), IMAP poll every 3 min, daily jobs. Postgres is the queue (`SELECT … FOR UPDATE SKIP LOCKED`) | Celery, ARQ, Redis, RabbitMQ, cron containers |
| Excel/CSV | `openpyxl` (read_only) + `csv` | pandas |
| Email | boto3 (SES v2), `smtplib`, `imap-tools`, `email-validator`, `dnspython` | extra email SaaS SDKs |
| Other | `holidays` (US holidays), `cryptography` (Fernet), `itsdangerous` (unsubscribe tokens), `argon2-cffi` (password hashing) | anything not clearly needed |
| Runtime glue | `uvicorn` (server), `psycopg[binary]` (Postgres driver), `python-multipart` (forms/uploads) | |
| Backups | worker runs `pg_dump` nightly, keeps 7 days on the VPS, uploads each dump to S3 with boto3 (bucket + keys in `.env`; 30-day lifecycle set on the bucket by Vikas) | cron containers, backup SaaS |
| Deploy | No Docker (Vikas, 27 Oct 2026): Ubuntu 22.04, Python 3.12 (deadsnakes), PostgreSQL 16 (PGDG repo), code + `.venv` in `/opt/outreach`, systemd units `deploy/outreach-web.service` + `deploy/outreach-worker.service`; existing Nginx proxies to 127.0.0.1:8000 | Docker, Kubernetes, separate frontend container |
| Tests | pytest on core logic only | UI tests, 100% coverage goals |

## v1 simplifications (agreed)

- Skip for v1: TOTP 2FA, third-party verification API, open/click tracking, daily summary email, template version tables (store the rendered subject/body on each `messages` row instead).
- Tags: a `text[]` column on `leads`, not separate tag tables.
- Reply from dashboard (FR-8.5): build it only after everything else works; it sends through the SAME sender and provider that sent the original email (SES identity or SMTP mailbox), keeps `In-Reply-To`/`References`, Reply-To = reply mailbox, and BCCs the reply mailbox.

## Must stay correct (core rules — keep them in one place each and test them)

1. Suppression check at send time (email and domain), not only at import.
2. Idempotent sending: unique `(enrollment_id, step_no)` on `messages`; insert the row before calling the provider so a crash never double-sends.
3. Send window in the lead's local timezone, business days only, US holidays skipped.
4. Caps: min(sender warm-up cap, sender daily cap, campaign daily cap), plus max 3 per recipient domain per day.
5. Stop rules: reply / bounce / complaint / unsubscribe / manual stop end the enrollment; optional stop for the whole company domain.
6. Auto-pause at bounce > 3% or complaint > 0.1% (last 24 h or 200 sends), evaluated only once the window has at least 50 sends.
7. Every email: footer (company + postal address + unsubscribe link), `List-Unsubscribe` + `List-Unsubscribe-Post` headers, unique `Message-ID`, Reply-To to the monitored mailbox; follow-ups use `In-Reply-To`/`References`.
8. SNS webhook signature verified; unsubscribe tokens signed.
9. Secrets (SMTP passwords, AWS keys) encrypted at rest, keys only in `.env`, never committed.

## Code style

- Simple, readable, boring code. Plain functions over classes; a class only where it removes real duplication (the two sending providers share one tiny interface: `send(msg, bcc=()) -> provider_message_id`).
- Suggested layout (keep it this flat): `app/main.py`, `config.py`, `db.py`, `models.py`, `auth.py`, `routes/<area>.py`, `services/` (importer, render, scheduler, providers, replies, events), `templates/`, `static/`, `worker.py`; `migrations/`; `tests/`.
- No abstraction "for the future", no generic base classes, no plugin systems, no feature flags, no event bus.
- Config via `.env` + one `config.py` (pydantic-settings). Provide `.env.example`.
- Every phase ends runnable. Add a one-command seed: `python -m app.seed` creates the first admin and demo data.

## How to work with me

- I am Vikas, owner of Avigronix. Explain decisions to me in simple Hinglish; code, comments and docs in English.
- Before a phase, say what you will build in a few lines. After a phase: what works, how to run/test it, what is left. Then stop and wait for my OK.
- If the SRS and these rules conflict, these rules win. If something is unclear or a choice is hard to undo, ask me instead of guessing.
- Do not add a library, service or file type outside this list without asking.
- Keep `docs/PLAN.md` updated with phase status (checkboxes).
