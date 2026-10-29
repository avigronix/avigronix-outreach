"""Numbers for the dashboard, campaign report and sender health (FR-10.x). Counted from messages/events/replies."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Campaign, Enrollment, Event, Lead, Message, Reply, SequenceStep, Sender
from app.services.scheduler import day_start, is_business_day
from app.services.warmup import WARMUP_DAYS, effective_daily_cap

UTC = timezone.utc
POSITIVE = ("interested", "meeting_booked")


def pct(part: int, whole: int) -> float:
    return 100 * part / whole if whole else 0.0


def message_stats(db: Session, *where) -> dict:
    """Sends and what happened to them, for the messages matching `where`."""
    msgs = select(Message.id).where(Message.status.in_(("sent", "failed")), *where).subquery()
    ids = select(msgs.c.id)
    sends = db.scalar(select(func.count()).select_from(msgs))
    events = dict(db.execute(
        select(Event.type, func.count(func.distinct(Event.message_id)))
        .where(Event.message_id.in_(ids)).group_by(Event.type)).all())
    replies = db.execute(
        select(func.count(func.distinct(Reply.lead_id)),
               func.count(func.distinct(Reply.lead_id)).filter(Reply.outcome.in_(POSITIVE)))
        .where(Reply.message_id.in_(ids), Reply.direction == "in", Reply.classification != "auto_reply")).one()
    unsubscribed = db.scalar(select(func.count(func.distinct(Message.lead_id))).join(Lead, Lead.id == Message.lead_id)
                             .where(Message.id.in_(ids), Lead.status == "unsubscribed"))
    s = {
        "sends": sends,
        "delivered": events.get("delivery", 0),
        "bounces": events.get("bounce_hard", 0),
        "complaints": events.get("complaint", 0),
        "replies": replies[0],
        "positive": replies[1],
        "unsubscribed": unsubscribed,
    }
    for key in ("delivered", "bounces", "complaints", "replies", "positive", "unsubscribed"):
        s[key + "_pct"] = pct(s[key], sends)
    return s


def bounce_level(value: float) -> str:
    return "green" if value < 2 else "amber" if value <= 3 else "red"


def complaint_level(value: float) -> str:
    return "green" if value < 0.05 else "amber" if value <= 0.1 else "red"


def sent_today(db: Session, now: datetime, cap_tz: str, *where) -> int:
    return db.scalar(select(func.count()).select_from(Message).where(
        Message.created_at >= day_start(now, cap_tz), Message.status != "failed", *where))


def daily_sends(db: Session, now: datetime, cap_tz: str, days: int = 10, *where) -> list[tuple[date, int]]:
    """Sends per day (cap timezone) for the last `days` business days, oldest first."""
    local_day = func.date(func.timezone(cap_tz, Message.created_at))
    counts = dict(db.execute(
        select(local_day, func.count()).where(Message.created_at >= now - timedelta(days=days * 2 + 7),
                                              Message.status != "failed", *where)
        .group_by(local_day)).all())
    result, d = [], now.astimezone(ZoneInfo(cap_tz)).date()
    while len(result) < days:
        if is_business_day(d):
            result.append((d, counts.get(d, 0)))
        d -= timedelta(days=1)
    return list(reversed(result))


def campaign_rows(db: Session) -> list[dict]:
    rows = []
    for c in db.scalars(select(Campaign).where(Campaign.status != "draft").order_by(Campaign.id.desc())):
        stats = message_stats(db, Message.campaign_id == c.id)
        rows.append({
            "c": c,
            "leads": db.scalar(select(func.count()).select_from(Enrollment).where(Enrollment.campaign_id == c.id)),
            "stats": stats,
            "next": db.scalar(select(func.min(Enrollment.next_send_at)).where(
                Enrollment.campaign_id == c.id, Enrollment.state == "active")),
        })
    return rows


def sender_rows(db: Session, now: datetime, cap_tz: str) -> list[dict]:
    rows = []
    for s in db.scalars(select(Sender).where(Sender.status != "disabled").order_by(Sender.type, Sender.from_email)):
        week = message_stats(db, Message.sender_id == s.id, Message.created_at >= now - timedelta(days=7))
        rows.append({"s": s, "today": sent_today(db, now, cap_tz, Message.sender_id == s.id),
                     "cap": effective_daily_cap(s), "week": week, "warmup_days": WARMUP_DAYS})
    return rows


def campaign_report(db: Session, campaign: Campaign) -> dict:
    """FR-10.2: totals, per step, and per subject variant of step 1."""
    steps = db.scalars(select(SequenceStep).where(SequenceStep.campaign_id == campaign.id)
                       .order_by(SequenceStep.step_no)).all()
    variants = db.scalars(select(Message.variant).where(Message.campaign_id == campaign.id, Message.step_no == 1)
                          .distinct().order_by(Message.variant)).all()
    return {
        "total": message_stats(db, Message.campaign_id == campaign.id),
        "steps": [(st, message_stats(db, Message.campaign_id == campaign.id, Message.step_no == st.step_no))
                  for st in steps],
        # A reply to any step counts for the variant its thread started with.
        "variants": [(v, message_stats(db, Message.campaign_id == campaign.id, Message.variant == v)) for v in variants],
    }
