import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import current_user, verify_csrf
from app.db import get_db
from app.models import (
    REPLY_OUTCOMES, SOURCE_TYPES, Campaign, Enrollment, Lead, Message, Reply, Sender, SequenceStep, User,
)
from app.services.audit import audit
from app.services.providers import SendError
from app.services.replies import send_manual_reply, tab_filter, visible
from app.services.suppression import suppress
from app.ui import flash, render

router = APIRouter(prefix="/replies", dependencies=[Depends(verify_csrf)])
log = logging.getLogger("replies")

TABS = {
    "needs_action": "Needs action",
    "interested": "Interested",
    "all": "All",
    "auto": "Auto-replies",
}
# Lead status after an outcome is set.
OUTCOME_STATUS = {"interested": "interested", "meeting_booked": "interested", "not_interested": "not_interested",
                  "wrong_person": "replied", "unsubscribe_request": "unsubscribed"}


def get_reply(db: Session, reply_id: int, user: User) -> Reply:
    reply = db.scalar(visible(user).where(Reply.id == reply_id))
    if reply is None:
        raise HTTPException(status_code=404, detail="Reply not found.")
    return reply


@router.get("")
def inbox(request: Request, tab: str = "needs_action", id: int | None = None, db: Session = Depends(get_db),
          user: User = Depends(current_user)):
    tab = tab if tab in TABS else "needs_action"
    counts = {t: db.scalar(select(func.count()).select_from(tab_filter(visible(user), t).subquery())) for t in TABS}
    rows = db.execute(
        tab_filter(visible(user), tab).add_columns(Lead).order_by(Reply.received_at.desc()).limit(100)).all()
    selected = get_reply(db, id, user) if id else (rows[0][0] if rows else None)

    context = {"tab": tab, "tabs": TABS, "counts": counts, "rows": rows, "selected": selected,
               "outcomes": REPLY_OUTCOMES, "now": datetime.now(timezone.utc)}
    if selected:
        lead = db.get(Lead, selected.lead_id)
        message = db.get(Message, selected.message_id) if selected.message_id else None
        campaign = db.get(Campaign, message.campaign_id) if message else None
        sent = db.scalars(select(Message).where(Message.lead_id == lead.id, Message.status == "sent")
                          .order_by(Message.sent_at)).all()
        replies = db.scalars(select(Reply).where(Reply.lead_id == lead.id).order_by(Reply.received_at)).all()
        thread = sorted([("sequence", m.sent_at, m) for m in sent] + [(r.direction, r.received_at, r) for r in replies],
                        key=lambda item: item[1])
        reply_sender = db.get(Sender, message.sender_id) if message else None
        steps_total = db.scalar(select(func.count()).select_from(SequenceStep).where(
            SequenceStep.campaign_id == campaign.id)) if campaign else 0
        enrollment = db.scalar(select(Enrollment).where(Enrollment.lead_id == lead.id)
                               .order_by(Enrollment.id.desc()).limit(1))
        company_stopped = db.scalar(select(func.count()).select_from(Enrollment).join(Lead, Lead.id == Enrollment.lead_id)
                                    .where(Lead.domain == lead.domain, Lead.id != lead.id,
                                           Enrollment.stop_reason == "company replied"))
        local = context["now"].astimezone(ZoneInfo(lead.timezone)) if lead.timezone else None
        context.update(
            lead=lead, message=message, campaign=campaign, thread=thread, sent_count=len(sent),
            steps_total=steps_total, enrollment=enrollment, company_stopped=company_stopped,
            lead_local=local.strftime("%I:%M %p").lstrip("0").lower() + local.strftime(" %Z") if local else "",
            users=db.scalars(select(User).where(User.is_active).order_by(User.name)).all(),
            reply_sender=reply_sender,
            senders_by_user={u.id: u.name for u in db.scalars(select(User))},
            source_types=SOURCE_TYPES,
        )
    return render(request, "replies.html", **context)


def back(reply: Reply, tab: str = "needs_action"):
    return RedirectResponse(f"/replies?tab={tab}&id={reply.id}", status_code=303)


@router.post("/{reply_id}/outcome")
def set_outcome(request: Request, reply_id: int, outcome: str = Form(...), tab: str = Form("needs_action"),
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    reply = get_reply(db, reply_id, user)
    if outcome not in REPLY_OUTCOMES:
        raise HTTPException(status_code=400, detail="Unknown outcome.")
    lead = db.get(Lead, reply.lead_id)
    reply.outcome, reply.handled_at = outcome, datetime.now(timezone.utc)
    if outcome == "unsubscribe_request":
        suppress(db, lead.email, "email", "unsubscribe", "reply asked to be removed", user.id)
    elif lead.status not in ("bounced", "complained", "unsubscribed", "do_not_contact"):
        lead.status = OUTCOME_STATUS[outcome]
    if reply.assigned_to is None:
        reply.assigned_to = user.id
    audit(db, request, user, "reply_outcome", "reply", reply.id, outcome=outcome)
    db.commit()
    flash(request, f"Marked as {REPLY_OUTCOMES[outcome]}.")
    return back(reply, tab)


@router.post("/{reply_id}/assign")
def assign(request: Request, reply_id: int, user_id: str = Form(""), tab: str = Form("needs_action"),
           db: Session = Depends(get_db), user: User = Depends(current_user)):
    reply = get_reply(db, reply_id, user)
    assignee = db.get(User, int(user_id)) if user_id.isdigit() else None
    reply.assigned_to = assignee.id if assignee else None
    if assignee:
        db.get(Lead, reply.lead_id).owner_id = assignee.id  # the lead follows the reply
    audit(db, request, user, "reply_assigned", "reply", reply.id, to=assignee.email if assignee else None)
    db.commit()
    flash(request, f"Assigned to {assignee.name}." if assignee else "Unassigned.")
    return back(reply, tab)


@router.post("/{reply_id}/suppress")
def suppress_sender(request: Request, reply_id: int, tab: str = Form("needs_action"),
                    db: Session = Depends(get_db), user: User = Depends(current_user)):
    reply = get_reply(db, reply_id, user)
    lead = db.get(Lead, reply.lead_id)
    suppress(db, lead.email, "email", "manual", "from replies inbox", user.id)
    if reply.outcome is None:
        reply.outcome, reply.handled_at = "unsubscribe_request", datetime.now(timezone.utc)
    audit(db, request, user, "suppression_added", "lead", lead.id, value=lead.email)
    db.commit()
    flash(request, f"{lead.email} added to the suppression list.")
    return back(reply, tab)


@router.post("/{reply_id}/send")
def send_reply(request: Request, reply_id: int, body: str = Form(""), action: str = Form("send"),
               tab: str = Form("needs_action"), db: Session = Depends(get_db), user: User = Depends(current_user)):
    """FR-8.5: answer from the dashboard (or just keep the text as a draft)."""
    reply = get_reply(db, reply_id, user)
    reply.draft = body.strip()
    db.commit()
    if action == "draft":
        flash(request, "Draft saved.")
        return back(reply, tab)
    try:
        sent = send_manual_reply(db, reply, user, body)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return back(reply, tab)
    except SendError as exc:
        flash(request, f"Not sent: {exc}. Your text is kept as a draft.", "error")
        return back(reply, tab)
    except Exception as exc:  # connection dropped mid-send: it may or may not have gone out
        log.exception("Manual reply %s: unknown result", reply.id)
        flash(request, f"The connection dropped while sending ({type(exc).__name__}). Check the sender's Sent "
                       "folder before trying again. Your text is kept as a draft.", "error")
        return back(reply, tab)
    audit(db, request, user, "reply_sent", "reply", sent.id, to=reply.from_email, sender=sent.from_email)
    db.commit()
    flash(request, f"Reply sent to {reply.from_email} from {sent.from_email}.")
    return back(reply, tab)
