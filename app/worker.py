"""The one background process: `python -m app.worker`.

A plain loop. Each job has an interval; a failing job is logged (and an alert emailed, at most once an
hour per job) and retried on its next turn, it never stops the loop. Jobs: send tick (5 s), lead imports
(5 s), heartbeat and auto-pause check (60 s), IMAP poll (3 min), daily jobs (checked every 5 min, run once a day).
"""

import logging
import signal
import time
import traceback
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import get_app_settings
from app.services.alerts import send_alert
from app.services.backup import run_backup
from app.services.events import check_auto_pause, clear_old_raw
from app.services.importer import run_import_jobs
from app.services.replies import poll_mailboxes
from app.services.scheduler import day_start, mark_inflight_unknown, run_send_tick
from app.services.warmup import daily_warmup_step

log = logging.getLogger("worker")

LOOP_SLEEP_SECONDS = 5
ALERT_EVERY_SECONDS = 3600
_stopping = False


def heartbeat() -> None:
    with SessionLocal() as db:
        get_app_settings(db).worker_heartbeat_at = datetime.now(timezone.utc)
        db.commit()


def daily_jobs() -> None:
    """Once per day, right after midnight in the cap timezone (America/New_York by default)."""
    with SessionLocal() as db:
        s = get_app_settings(db)
        now = datetime.now(timezone.utc)
        today_start = day_start(now, s.cap_day_tz)
        if s.last_daily_run and s.last_daily_run >= today_start:
            return
        yesterday_start = day_start(today_start - timedelta(hours=1), s.cap_day_tz)
        for note in daily_warmup_step(db, today_start, yesterday_start):
            log.info("Warm-up: %s", note)
        log.info("Cleared raw payloads of %s old events", clear_old_raw(db, now))

        s = get_app_settings(db)
        try:
            s.last_backup_note = run_backup(now)
            s.last_backup_at = now
            log.info("Backup: %s", s.last_backup_note)
        except RuntimeError as exc:
            s.last_backup_note = f"FAILED: {exc}"
            send_alert("Backup failed", str(exc))
        s.last_daily_run = now
        db.commit()


# (name, every N seconds, function)
JOBS = [
    ("heartbeat", 60, heartbeat),
    ("auto_pause", 60, check_auto_pause),
    ("imap", 180, poll_mailboxes),
    ("daily", 300, daily_jobs),
    ("send", 5, run_send_tick),
    ("imports", 5, run_import_jobs),
]


def _stop(signum, frame) -> None:
    global _stopping
    log.info("Stop signal received, finishing current job")
    _stopping = True


def run() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    last_run: dict[str, float] = {}
    last_alert: dict[str, float] = {}
    with SessionLocal() as db:
        cut_off = mark_inflight_unknown(db)
    if cut_off:
        log.warning("%s send(s) were cut off by the last stop; marked 'unknown' for a human to check", cut_off)
        send_alert("Sends with unknown result",
                   f"{cut_off} email(s) were being sent when the worker stopped. Check them on the campaign page.")
    log.info("Worker started")

    while not _stopping:
        for name, every, job in JOBS:
            if time.monotonic() - last_run.get(name, float("-inf")) < every:
                continue
            last_run[name] = time.monotonic()
            try:
                job()
            except Exception:
                log.exception("Job %s failed", name)
                if time.monotonic() - last_alert.get(name, float("-inf")) > ALERT_EVERY_SECONDS:
                    last_alert[name] = time.monotonic()
                    send_alert(f"Worker job '{name}' failed", traceback.format_exc()[-2000:])
            if _stopping:
                break
        time.sleep(LOOP_SLEEP_SECONDS)

    log.info("Worker stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run()
