from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import current_user
from app.db import get_db
from app.models import Campaign, Enrollment, Lead, Message, Reply, Sender, User, get_app_settings
from app.services import reports
from app.services.alerts import send_alert
from app.services.replies import tab_filter, visible
from app.services.scheduler import next_day_start
from app.services.warmup import effective_daily_cap
from app.ui import render

router = APIRouter()

WORKER_STALE_AFTER = timedelta(minutes=5)
WORKER_ALERT_EVERY = timedelta(hours=1)


@router.get("/")
def dashboard(request: Request, background: BackgroundTasks, db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    s = get_app_settings(db)
    now = datetime.now(timezone.utc)
    heartbeat = s.worker_heartbeat_at
    worker_ok = heartbeat is not None and now - heartbeat < WORKER_STALE_AFTER
    if not worker_ok and (s.worker_alerted_at is None or now - s.worker_alerted_at > WORKER_ALERT_EVERY):
        # A dead worker can't report itself: the first dashboard visit sends the alert (at most hourly).
        s.worker_alerted_at = now
        db.commit()
        background.add_task(send_alert, "Worker not running",
                            f"Last heartbeat: {heartbeat or 'never'} (UTC). No emails are being sent. "
                            "Check: systemctl status outreach-worker / journalctl -u outreach-worker")

    auto_paused = [(f"/campaigns/{c.id}", c.name, c.pause_reason) for c in db.scalars(
        select(Campaign).where(Campaign.status == "paused", Campaign.pause_reason.like("Auto-paused%")))]
    auto_paused += [(f"/senders/{x.id}", x.from_email, x.pause_reason) for x in db.scalars(
        select(Sender).where(Sender.status == "paused", Sender.pause_reason.like("Auto-paused%")))]
    needs_action = db.execute(tab_filter(visible(user), "needs_action").add_columns(Lead)
                              .order_by(Reply.received_at.desc()).limit(4)).all()
    context = {"now": now, "heartbeat": heartbeat, "worker_ok": worker_ok, "auto_paused": auto_paused,
               "needs_action": needs_action}

    if user.role in ("admin", "manager"):
        week_ago = now - timedelta(days=7)
        active = db.scalars(select(Sender).where(Sender.status == "active")).all()
        ses_ids = [x.id for x in db.scalars(select(Sender).where(Sender.type == "ses"))] or [0]
        cap = sum(effective_daily_cap(x) for x in active)
        days = reports.daily_sends(db, now, s.cap_day_tz, 10)
        context.update(
            sent_today=reports.sent_today(db, now, s.cap_day_tz),
            cap_today=cap,
            queued=db.scalar(select(func.count()).select_from(Enrollment)
                             .join(Campaign, Campaign.id == Enrollment.campaign_id)
                             .where(Enrollment.state == "active", Campaign.status == "running",
                                    Enrollment.next_send_at < next_day_start(now, s.cap_day_tz))),
            week=reports.message_stats(db, Message.created_at >= week_ago),
            ses_week=reports.message_stats(db, Message.created_at >= week_ago, Message.sender_id.in_(ses_ids)),
            days=days,
            chart_max=max([n for _, n in days] + [cap, 1]),
            replies_week=db.scalar(select(func.count()).select_from(Reply).where(
                Reply.received_at >= week_ago, Reply.direction == "in", Reply.classification != "auto_reply")),
            interested_week=db.scalar(select(func.count()).select_from(Reply).where(
                Reply.received_at >= week_ago, Reply.outcome.in_(reports.POSITIVE))),
            need_action_total=db.scalar(select(func.count()).select_from(
                tab_filter(visible(user), "needs_action").subquery())),
            campaigns=reports.campaign_rows(db)[:6],
            sender_rows=reports.sender_rows(db, now, s.cap_day_tz),
            bounce_level=reports.bounce_level,
            complaint_level=reports.complaint_level,
            limits=s,
        )
    return render(request, "dashboard.html", **context)
