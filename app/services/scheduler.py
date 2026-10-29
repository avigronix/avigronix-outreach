"""The send engine (core rules 1–5 live here). Worker job: run_send_tick(), every 5 seconds
(SES senders space sends 2–6 s apart; a 60 s tick would cap each sender at 60 emails an hour).

One tick:
  1. scheduled campaigns whose start time passed become running
  2. retry pass: messages in 'retry' whose time has come
  3. due enrollments (state active, next_send_at <= now, campaign running), each in its own transaction:
     stop checks → suppression → send window → sender → caps → render → INSERT message (commit) → send
"""

import logging
import random
from datetime import date, datetime, time, timedelta, timezone
from email.utils import make_msgid
from zoneinfo import ZoneInfo

import holidays
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    AppSetting, Campaign, CampaignSender, Enrollment, Lead, Message, Sender, SequenceStep, Template,
    get_app_settings,
)
from app.services.crypto import unsubscribe_url
from app.services.events import record_event
from app.services.providers import SendError, provider_for
from app.services.render import build_message, lead_values, render_content, reply_subject
from app.services.suppression import is_suppressed, suppress
from app.services.validation import is_free_mail
from app.services.warmup import effective_daily_cap

log = logging.getLogger("scheduler")

US_HOLIDAYS = holidays.US()
UTC = timezone.utc
DUE_BATCH = 200
MAX_ATTEMPTS = 3
RETRY_BACKOFF_MIN = [5, 15, 45]  # after attempt 1, 2 (attempt 3 failing = failed)
STOP_STATUSES = {"replied", "interested", "not_interested", "bounced", "unsubscribed", "complained",
                 "do_not_contact"}


# ---------- time and windows (pure) ----------

def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_hm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in US_HOLIDAYS


def is_send_day(d: date, weekdays: list[int]) -> bool:
    return d.weekday() in weekdays and d not in US_HOLIDAYS


def windows_of(schedule: dict) -> list[tuple[time, time]]:
    return sorted((parse_hm(a), parse_hm(b)) for a, b in schedule["windows"])


def in_window(now: datetime, tz: ZoneInfo, schedule: dict) -> bool:
    local = now.astimezone(tz)
    if not is_send_day(local.date(), schedule["weekdays"]):
        return False
    return any(start <= local.time() < end for start, end in windows_of(schedule))


def next_window_start(after: datetime, tz: ZoneInfo, schedule: dict) -> datetime:
    """Earliest moment >= `after` that is inside a send window (in the lead's local time), as UTC."""
    local = after.astimezone(tz)
    for offset in range(0, 22):
        day = local.date() + timedelta(days=offset)
        if not is_send_day(day, schedule["weekdays"]):
            continue
        for start, end in windows_of(schedule):
            start_dt = datetime.combine(day, start, tzinfo=tz)
            end_dt = datetime.combine(day, end, tzinfo=tz)
            if local < start_dt:
                return start_dt.astimezone(UTC)
            if start_dt <= local < end_dt:
                return after.astimezone(UTC)
    raise ValueError("No send window in the next 3 weeks — check the campaign's weekdays.")


def add_business_days(d: date, n: int) -> date:
    """Mon–Fri, US holidays skipped."""
    while n > 0:
        d += timedelta(days=1)
        if is_business_day(d):
            n -= 1
    return d


def next_step_time(sent_at: datetime, delay_days: int, tz: ZoneInfo, schedule: dict,
                   rng: random.Random = random) -> datetime:
    """When the next step is due: N business days after the send (lead's local date), at a window start
    plus 0–45 random minutes so a batch doesn't all go out at 9:00 sharp."""
    target = add_business_days(sent_at.astimezone(tz).date(), delay_days)
    start = next_window_start(datetime.combine(target, time(0), tzinfo=tz), tz, schedule)
    return start + timedelta(minutes=rng.randint(0, 45))


def day_start(now: datetime, tz_name: str) -> datetime:
    """Midnight of 'today' in the cap timezone, as UTC (daily caps reset here)."""
    tz = ZoneInfo(tz_name)
    return datetime.combine(now.astimezone(tz).date(), time(0), tzinfo=tz).astimezone(UTC)


def next_day_start(now: datetime, tz_name: str) -> datetime:
    tz = ZoneInfo(tz_name)
    tomorrow = now.astimezone(tz).date() + timedelta(days=1)
    return datetime.combine(tomorrow, time(0), tzinfo=tz).astimezone(UTC)


def lead_zone(lead: Lead, campaign: Campaign) -> ZoneInfo:
    return ZoneInfo(lead.timezone or campaign.schedule.get("default_tz") or "America/New_York")


# ---------- tick context: today's counts, loaded once per tick ----------

class TickContext:
    def __init__(self, db: Session, now: datetime, s: AppSetting):
        self.now = now
        self.domain_max = s.domain_daily_max
        self.company, self.postal_address = s.company_name, s.postal_address
        self.since = day_start(now, s.cap_day_tz)
        self.tomorrow = next_day_start(now, s.cap_day_tz)
        counted = (Message.created_at >= self.since, Message.status != "failed")
        self.sender_counts = dict(db.execute(
            select(Message.sender_id, func.count()).where(*counted).group_by(Message.sender_id)).all())
        self.campaign_counts = dict(db.execute(
            select(Message.campaign_id, func.count()).where(*counted).group_by(Message.campaign_id)).all())
        self.domain_counts: dict[str, int] = {}
        self.pools: dict[int, list[Sender]] = {}
        self.busy: set[int] = set()  # sender ids, or -campaign_id for a whole step-1 pool, waiting on the gap

    def domain_count(self, db: Session, domain: str) -> int:
        if domain not in self.domain_counts:
            self.domain_counts[domain] = db.scalar(select(func.count()).select_from(Message).where(
                Message.to_domain == domain, Message.created_at >= self.since, Message.status != "failed"))
        return self.domain_counts[domain]

    def pool(self, db: Session, campaign: Campaign) -> list[Sender]:
        if campaign.id not in self.pools:
            self.pools[campaign.id] = db.scalars(
                select(Sender).join(CampaignSender, CampaignSender.sender_id == Sender.id)
                .where(CampaignSender.campaign_id == campaign.id).order_by(Sender.id)).all()
        return self.pools[campaign.id]

    def count_sent(self, message: Message, delta: int = 1) -> None:
        self.sender_counts[message.sender_id] = self.sender_counts.get(message.sender_id, 0) + delta
        self.campaign_counts[message.campaign_id] = self.campaign_counts.get(message.campaign_id, 0) + delta
        self.domain_counts[message.to_domain] = self.domain_counts.get(message.to_domain, 0) + delta


def room_left(sender: Sender, ctx: TickContext) -> int:
    return effective_daily_cap(sender) - ctx.sender_counts.get(sender.id, 0)


def pick_sender(db: Session, enr: Enrollment, campaign: Campaign, ctx: TickContext) -> tuple[Sender | None, str]:
    """Step 1: the pool sender with the most room left. Follow-ups: the same sender (keeps the thread).
    Returns (sender, "") or (None, reason) with reason in: no_sender / capped / busy."""
    candidates = [db.get(Sender, enr.sender_id)] if enr.sender_id else ctx.pool(db, campaign)
    usable = [s for s in candidates if s.status == "active" and s.type == campaign.provider]
    if not usable:
        return None, "no_sender"
    with_room = [s for s in usable if room_left(s, ctx) > 0]
    if not with_room:
        return None, "capped"
    ready = [s for s in with_room if s.next_send_at is None or s.next_send_at <= ctx.now]
    if not ready:
        return None, "busy"
    return max(ready, key=lambda s: room_left(s, ctx)), ""


# ---------- one enrollment ----------

def stop_enrollment(enr: Enrollment, reason: str) -> None:
    enr.state, enr.stop_reason, enr.next_send_at = "stopped", reason[:40], None


def push_due(db: Session, until: datetime, *where) -> None:
    """Move every due enrollment matching `where` to `until` (e.g. a campaign that hit today's cap)."""
    db.execute(update(Enrollment).where(Enrollment.state == "active", Enrollment.next_send_at < until, *where)
               .values(next_send_at=until))


def create_message(db: Session, enr: Enrollment, campaign: Campaign, step: SequenceStep, lead: Lead,
                   sender: Sender, ctx: TickContext) -> Message:
    """Render the step for this lead and add the message row (status 'sending'). Caller commits."""
    template = db.get(Template, step.template_id)
    previous = db.scalars(select(Message).where(Message.enrollment_id == enr.id, Message.status == "sent")
                          .order_by(Message.step_no)).all()
    if step.same_thread and previous:
        subject, variant = reply_subject(previous[0].subject), previous[0].variant
        in_reply_to = previous[-1].message_id_header
        references = " ".join(m.message_id_header for m in previous)
    else:
        index = enr.id % len(template.subject_variants)  # even A/B/C rotation
        subject, variant = template.subject_variants[index], "ABC"[index]
        in_reply_to = references = ""

    rendered = render_content(subject, template.body_text, template.body_html, lead_values(lead, sender.from_name),
                              ctx.company, ctx.postal_address, unsubscribe_url(lead.email))
    message = Message(
        enrollment_id=enr.id, campaign_id=campaign.id, lead_id=lead.id, sender_id=sender.id, step_no=step.step_no,
        variant=variant, to_email=lead.email, to_domain=lead.domain, subject=rendered["subject"],
        body_text=rendered["text"], body_html=rendered["html"], message_id_header=make_msgid(domain=sender.domain),
        in_reply_to=in_reply_to, references=references, status="sending", attempts=1,
    )
    db.add(message)
    enr.sender_id = enr.sender_id or sender.id
    enr.next_send_at = None  # not due again until this send has an outcome
    return message


def mime_for(message: Message, sender: Sender):
    """The exact email for a message row (same Message-ID on every attempt)."""
    return build_message(
        from_name=sender.from_name, from_email=sender.from_email, to_email=message.to_email,
        reply_to=sender.reply_to, subject=message.subject, text=message.body_text, html_body=message.body_html,
        unsubscribe_link=unsubscribe_url(message.to_email), unsubscribe_mailto=sender.reply_to,
        in_reply_to=message.in_reply_to or None, references=message.references or None,
        message_id=message.message_id_header,
    )


def advance(db: Session, enr: Enrollment, campaign: Campaign, lead: Lead, sent_at: datetime) -> None:
    next_step = db.scalar(select(SequenceStep).where(SequenceStep.campaign_id == campaign.id,
                                                     SequenceStep.step_no > enr.current_step)
                          .order_by(SequenceStep.step_no).limit(1))
    if next_step is None:
        enr.state, enr.next_send_at = "completed", None
        return
    enr.current_step = next_step.step_no
    enr.next_send_at = next_step_time(sent_at, next_step.delay_business_days, lead_zone(lead, campaign),
                                      campaign.schedule)


def mark_sent(db: Session, message: Message, provider_id: str, sent_at: datetime) -> None:
    enr = db.get(Enrollment, message.enrollment_id)
    lead = db.get(Lead, message.lead_id)
    message.status, message.sent_at, message.provider_message_id = "sent", sent_at, provider_id
    message.retry_at, message.error = None, ""
    enr.last_sent_at = sent_at
    if enr.state == "active":
        advance(db, enr, db.get(Campaign, message.campaign_id), lead, sent_at)
    if lead.status == "new":
        lead.status = "in_sequence"


def handle_send_error(db: Session, message: Message, exc: SendError, sender: Sender, ctx: TickContext) -> None:
    enr = db.get(Enrollment, message.enrollment_id)
    message.error = str(exc)[:2000]
    if exc.kind == "temporary" and message.attempts < MAX_ATTEMPTS:
        message.status = "retry"
        message.retry_at = ctx.now + timedelta(minutes=RETRY_BACKOFF_MIN[message.attempts - 1])
    elif exc.kind == "sender":
        # Nothing went out and the sender itself is broken: forget this attempt, pause the sender, the lead waits.
        ctx.count_sent(message, -1)
        if message.step_no == 1:
            enr.sender_id = None  # step 1 may go out through another pool sender
        db.delete(message)
        enr.next_send_at = ctx.now
        sender.status, sender.pause_reason = "paused", f"Auto-paused: {exc}"[:255]
        log.error("Sender %s paused: %s", sender.from_email, exc)
    elif exc.kind == "recipient":
        message.status = "failed"
        record_event(db, "bounce_hard", message=message, occurred_at=ctx.now, detail=str(exc))
        suppress(db, message.to_email, "email", "hard_bounce", "rejected at send")
        stop_enrollment(enr, "bounced")
    else:  # "message", or still temporary after the last attempt
        message.status = "failed"
        stop_enrollment(enr, "send_failed")


def deliver(db: Session, message: Message, sender: Sender, ctx: TickContext) -> None:
    """Call the provider for a committed message row and record the outcome."""
    try:
        provider_id = provider_for(sender).send(mime_for(message, sender))
    except SendError as exc:
        handle_send_error(db, message, exc, sender, ctx)
    except Exception as exc:
        # We can't tell whether it went out. Never resend automatically: a human decides.
        log.exception("Unknown result for message %s", message.id)
        message.status, message.error = "unknown", f"{type(exc).__name__}: {exc}"[:2000]
    else:
        mark_sent(db, message, provider_id, utcnow())
    sender.next_send_at = utcnow() + timedelta(seconds=random.randint(sender.gap_min_sec, sender.gap_max_sec))
    db.commit()


def process_enrollment(db: Session, enrollment_id: int, ctx: TickContext) -> None:
    enr = db.scalar(select(Enrollment).where(Enrollment.id == enrollment_id, Enrollment.state == "active")
                    .with_for_update(skip_locked=True))
    if enr is None or enr.next_send_at is None or enr.next_send_at > ctx.now:
        db.rollback()
        return
    if (enr.sender_id or -enr.campaign_id) in ctx.busy:
        db.rollback()  # its sender (or its whole step-1 pool) is waiting for the gap between sends
        return
    campaign = db.get(Campaign, enr.campaign_id)
    lead = db.get(Lead, enr.lead_id)

    # Stop rules (core rule 5) and suppression (core rule 1), checked right before sending.
    if lead.status in STOP_STATUSES:
        stop_enrollment(enr, lead.status)
        db.commit()
        return
    if is_suppressed(db, lead.email):
        stop_enrollment(enr, "suppressed")
        db.commit()
        return
    step = db.scalar(select(SequenceStep).where(SequenceStep.campaign_id == campaign.id,
                                                SequenceStep.step_no == enr.current_step))
    if step is None:
        enr.state, enr.next_send_at = "completed", None
        db.commit()
        return

    # Send window in the lead's local time (core rule 3).
    tz = lead_zone(lead, campaign)
    if not in_window(ctx.now, tz, campaign.schedule):
        enr.next_send_at = next_window_start(ctx.now, tz, campaign.schedule) + timedelta(minutes=random.randint(0, 45))
        db.commit()
        return

    # Caps (core rule 4): campaign, sender (warm-up + daily), recipient domain.
    if ctx.campaign_counts.get(campaign.id, 0) >= campaign.daily_cap:
        push_due(db, ctx.tomorrow, Enrollment.campaign_id == campaign.id)
        db.commit()
        return
    sender, why = pick_sender(db, enr, campaign, ctx)
    if sender is None:
        if why == "busy":
            ctx.busy.add(enr.sender_id or -campaign.id)
            db.rollback()
        elif why == "capped":
            if enr.sender_id:
                push_due(db, ctx.tomorrow, Enrollment.sender_id == enr.sender_id)
            else:
                push_due(db, ctx.tomorrow, Enrollment.campaign_id == campaign.id, Enrollment.sender_id.is_(None))
            db.commit()
        else:  # no active sender right now: look again in 30 minutes
            enr.next_send_at = ctx.now + timedelta(minutes=30)
            db.commit()
        return
    if not is_free_mail(lead.domain) and ctx.domain_count(db, lead.domain) >= ctx.domain_max:
        enr.next_send_at = ctx.tomorrow
        db.commit()
        return

    # Idempotency (core rule 2): the row is committed before the provider is called.
    message = create_message(db, enr, campaign, step, lead, sender, ctx)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        log.warning("Enrollment %s step %s already has a message; not sending again", enr.id, enr.current_step)
        return
    ctx.count_sent(message)
    deliver(db, message, sender, ctx)


# ---------- retries, campaign states, the tick ----------

def process_retry(db: Session, message_id: int, ctx: TickContext) -> None:
    message = db.scalar(select(Message).where(Message.id == message_id, Message.status == "retry")
                        .with_for_update(skip_locked=True))
    if message is None:
        db.rollback()
        return
    enr = db.get(Enrollment, message.enrollment_id)
    campaign = db.get(Campaign, message.campaign_id)
    lead = db.get(Lead, message.lead_id)
    sender = db.get(Sender, message.sender_id)
    if enr.state != "active" or lead.status in STOP_STATUSES or is_suppressed(db, lead.email):
        message.status, message.error = "failed", "Stopped before retry. " + message.error
        if enr.state == "active":
            stop_enrollment(enr, lead.status if lead.status in STOP_STATUSES else "suppressed")
        db.commit()
        return
    if campaign.status != "running" or sender.status != "active" or \
            (sender.next_send_at is not None and sender.next_send_at > ctx.now):
        db.rollback()
        return
    tz = lead_zone(lead, campaign)
    if not in_window(ctx.now, tz, campaign.schedule):
        message.retry_at = next_window_start(ctx.now, tz, campaign.schedule)
        db.commit()
        return
    message.attempts += 1
    message.status = "sending"
    db.commit()
    deliver(db, message, sender, ctx)


def update_campaign_states(db: Session, now: datetime) -> None:
    db.execute(update(Campaign).where(Campaign.status == "scheduled", Campaign.start_at <= now)
               .values(status="running", started_at=now))
    active = select(Enrollment.id).where(Enrollment.campaign_id == Campaign.id, Enrollment.state == "active")
    db.execute(update(Campaign).where(Campaign.status == "running", ~active.exists()).values(status="completed"))
    db.commit()


def mark_inflight_unknown(db: Session) -> int:
    """At worker start: rows still 'sending' were cut off mid-send. We can't know if they went out."""
    count = db.execute(update(Message).where(Message.status == "sending")
                       .values(status="unknown", error="Worker stopped while sending")).rowcount
    db.commit()
    return count


def run_send_tick(db: Session | None = None, now: datetime | None = None) -> None:
    own_session = db is None
    db = db or SessionLocal()
    try:
        now = now or utcnow()
        update_campaign_states(db, now)
        ctx = TickContext(db, now, get_app_settings(db))
        retry_ids = db.scalars(select(Message.id).where(Message.status == "retry", Message.retry_at <= now)
                               .order_by(Message.retry_at).limit(50)).all()
        for message_id in retry_ids:
            process_retry(db, message_id, ctx)
        due_ids = db.scalars(
            select(Enrollment.id).join(Campaign, Campaign.id == Enrollment.campaign_id)
            .where(Enrollment.state == "active", Enrollment.next_send_at <= now, Campaign.status == "running")
            .order_by(Enrollment.next_send_at).limit(DUE_BATCH)).all()
        for enrollment_id in due_ids:
            process_enrollment(db, enrollment_id, ctx)
    finally:
        if own_session:
            db.close()
