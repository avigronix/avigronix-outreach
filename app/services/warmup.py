"""Warm-up caps per sender (SRS 7.3) and the daily step that moves `warmup_day` forward."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Event, Message, Sender

WARMUP_DAYS = 28

# Emails/day for warm-up days 1..28.
WARMUP_TABLE = {
    "ses": [50, 60, 70, 80, 90, 100, 100,
            150, 175, 200, 225, 250, 275, 300,
            400, 450, 500, 550, 600, 650, 700,
            800, 850, 900, 1000, 1050, 1100, 1200],
    "smtp": [10, 11, 12, 13, 14, 15, 15,
             20, 21, 22, 23, 24, 25, 25,
             30, 32, 34, 36, 38, 40, 40,
             40, 42, 44, 46, 48, 50, 50],
}
# After day 28: SES grows 100/day up to 2,000; SMTP mailboxes stay at 50.
WARMUP_MAX = {"ses": 2000, "smtp": 50}

DEFAULTS = {
    # type: (daily cap, min gap s, max gap s)
    "ses": (2000, 2, 6),
    "smtp": (50, 60, 180),
}


def warmup_cap(sender_type: str, day: int) -> int:
    day = max(1, day)
    if day <= WARMUP_DAYS:
        return WARMUP_TABLE[sender_type][day - 1]
    if sender_type == "ses":
        return min(WARMUP_MAX["ses"], WARMUP_TABLE["ses"][-1] + 100 * (day - WARMUP_DAYS))
    return WARMUP_MAX["smtp"]


def effective_daily_cap(sender: Sender) -> int:
    """min(warm-up cap or admin override, sender daily cap). The campaign cap is applied by the scheduler."""
    warm = sender.warmup_override_cap or warmup_cap(sender.type, sender.warmup_day)
    return min(warm, sender.daily_cap)


# Healthy enough to move up a warm-up day (SRS 7.3: otherwise the cap holds).
STEP_MAX_BOUNCE_PCT = 2.0
STEP_MAX_COMPLAINT_PCT = 0.1


def yesterday_stats(db: Session, sender_id: int, start: datetime, end: datetime) -> tuple[int, int, int]:
    """(sends, hard bounces, complaints) for one sender between start and end."""
    ids = select(Message.id).where(Message.sender_id == sender_id, Message.status.in_(("sent", "failed")),
                                   Message.created_at >= start, Message.created_at < end).subquery()
    sends = db.scalar(select(func.count()).select_from(ids))
    counts = dict(db.execute(
        select(Event.type, func.count(func.distinct(Event.message_id)))
        .where(Event.message_id.in_(select(ids.c.id)), Event.type.in_(("bounce_hard", "complaint")))
        .group_by(Event.type)).all())
    return sends, counts.get("bounce_hard", 0), counts.get("complaint", 0)


def daily_warmup_step(db: Session, today_start: datetime, yesterday_start: datetime) -> list[str]:
    """Daily job: each active sender that sent yesterday with healthy numbers moves up one warm-up day.
    No sends yesterday (weekend, paused) or unhealthy numbers → the day (and cap) holds."""
    notes = []
    for sender in db.scalars(select(Sender).where(Sender.status == "active")).all():
        sends, bounces, complaints = yesterday_stats(db, sender.id, yesterday_start, today_start)
        healthy = sends > 0 and 100 * bounces / sends < STEP_MAX_BOUNCE_PCT \
            and 100 * complaints / sends < STEP_MAX_COMPLAINT_PCT
        if healthy:
            sender.warmup_day += 1
        notes.append(f"{sender.from_email}: {sends} sent, {bounces} bounced, {complaints} complaints → "
                     f"{'day ' + str(sender.warmup_day) if healthy else 'holds at day ' + str(sender.warmup_day)}")
    db.commit()
    return notes
