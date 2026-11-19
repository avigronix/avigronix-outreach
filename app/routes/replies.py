import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import current_user, verify_csrf
from app.db import get_db
from app.models import (
    REPLY_OUTCOMES, SOURCE_TYPES, Campaign, Enrollment, Lead, Message, Reply, ReplySnippet, Sender, SequenceStep, User,
)
from app.services.audit import audit
from app.services.providers import SendError
from app.services.render import lead_values, merge
from app.services.replies import (
    NEWEST_FIRST, ReplyTaken, claim, next_in_tab, send_manual_reply, tab_filter, visible,
)
from app.services.suppression import suppress
from app.ui import flash, render, today_ist

router = APIRouter(prefix="/replies", dependencies=[Depends(verify_csrf)])
log = logging.getLogger("replies")

TABS = {
    "needs_action": "Needs action",
    "mine": "Mine",
    "unclaimed": "Unclaimed",
    "interested": "Interested",
    "all": "All",
    "auto": "Auto-replies",
}
# Lead status after an outcome is set.
OUTCOME_STATUS = {"interested": "interested", "meeting_booked": "interested", "not_interested": "not_interested",
                  "wrong_person": "replied", "unsubscribe_request": "unsubscribed"}
# Lead statuses an outcome (or its Undo) never overwrites.
DONE_STATUSES = ("bounced", "complained", "unsubscribed", "do_not_contact")


def get_reply(db: Session, reply_id: int, user: User) -> Reply:
    reply = db.scalar(visible(user).where(Reply.id == reply_id))
    if reply is None:
        other = db.scalar(select(User).join(Reply, Reply.assigned_to == User.id).where(Reply.id == reply_id))
        if other is not None:  # a teammate took it while this page was open
            raise ReplyTaken(other.name)
        raise HTTPException(status_code=404, detail="Reply not found.")
    return reply


@router.get("")
def inbox(request: Request, tab: str = "needs_action", id: int | None = None, db: Session = Depends(get_db),
          user: User = Depends(current_user)):
    tab = tab if tab in TABS else "needs_action"
    counts = {t: db.scalar(select(func.count()).select_from(tab_filter(visible(user), t, user).subquery()))
              for t in TABS}
    rows = db.execute(
        tab_filter(visible(user), tab, user).add_columns(Lead).order_by(*NEWEST_FIRST).limit(100)).all()
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
        values = lead_values(lead, reply_sender.from_name if reply_sender else "")
        context.update(
            lead=lead, message=message, campaign=campaign, thread=thread, sent_count=len(sent),
            steps_total=steps_total, enrollment=enrollment, company_stopped=company_stopped,
            users=db.scalars(select(User).where(User.is_active).order_by(User.name)).all(),
            reply_sender=reply_sender,
            senders_by_user={u.id: u.name for u in db.scalars(select(User))},
            today=today_ist(),
            source_types=SOURCE_TYPES,
            # Ready-made replies with this lead's merge fields already filled in.
            snippets=[(s.title, merge(s.body, values)[0])
                      for s in db.scalars(select(ReplySnippet).order_by(ReplySnippet.title))],
        )
    return render(request, "replies.html", **context)


def back(reply: Reply, tab: str = "needs_action"):
    return RedirectResponse(f"/replies?tab={tab}&id={reply.id}", status_code=303)


def next_after(db: Session, user: User, reply: Reply, tab: str):
    """A handled reply leaves "Needs action", so that tab opens the next one; other tabs stay on this reply."""
    if tab != "needs_action":
        return back(reply, tab)
    nxt = next_in_tab(db, user, tab, reply)
    return RedirectResponse(f"/replies?tab={tab}" + (f"&id={nxt.id}" if nxt else ""), status_code=303)


@router.post("/{reply_id}/outcome")
def set_outcome(request: Request, reply_id: int, outcome: str = Form(...), tab: str = Form("needs_action"),
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    reply = get_reply(db, reply_id, user)
    if outcome not in REPLY_OUTCOMES:
        raise HTTPException(status_code=400, detail="Unknown outcome.")
    assigned_before = reply.assigned_to
    claim(db, reply, user)
    lead = db.get(Lead, reply.lead_id)
    before = {"reply": reply.id, "tab": tab, "outcome": reply.outcome, "lead_status": lead.status,
              "assigned_to": assigned_before,
              "handled_at": reply.handled_at.isoformat() if reply.handled_at else None}
    reply.outcome, reply.handled_at = outcome, datetime.now(timezone.utc)
    if outcome == "unsubscribe_request":
        suppress(db, lead.email, "email", "unsubscribe", "reply asked to be removed", user.id)
    elif lead.status not in DONE_STATUSES:
        lead.status = OUTCOME_STATUS[outcome]
    audit(db, request, user, "reply_outcome", "reply", reply.id, outcome=outcome)
    db.commit()
    undo = None
    if outcome != "unsubscribe_request":  # a suppression is only ever removed by an admin, never by Undo
        request.session["undo_outcome"] = before
        undo = f"/replies/{reply.id}/undo"
    flash(request, f"{reply.from_email} marked as {REPLY_OUTCOMES[outcome]}.", undo=undo)
    return next_after(db, user, reply, tab)


@router.post("/{reply_id}/undo")
def undo_outcome(request: Request, reply_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """The toast's Undo: put back the outcome, owner and lead status the last outcome replaced."""
    before = request.session.pop("undo_outcome", None)
    reply = get_reply(db, reply_id, user)
    if not before or before["reply"] != reply.id:
        flash(request, "Nothing to undo.", "error")
        return back(reply, "all")
    lead = db.get(Lead, reply.lead_id)
    reply.outcome, reply.assigned_to = before["outcome"], before["assigned_to"]
    reply.handled_at = datetime.fromisoformat(before["handled_at"]) if before["handled_at"] else None
    if lead.status not in DONE_STATUSES:  # bounced/unsubscribed since then: that wins
        lead.status = before["lead_status"]
    audit(db, request, user, "reply_outcome_undone", "reply", reply.id, outcome=before["outcome"])
    db.commit()
    flash(request, "Undone.")
    return back(reply, before["tab"])


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
    claim(db, reply, user)
    lead = db.get(Lead, reply.lead_id)
    suppress(db, lead.email, "email", "manual", "from replies inbox", user.id)
    if reply.outcome is None:
        reply.outcome, reply.handled_at = "unsubscribe_request", datetime.now(timezone.utc)
    audit(db, request, user, "suppression_added", "lead", lead.id, value=lead.email)
    db.commit()
    flash(request, f"{lead.email} added to the suppression list.")
    return next_after(db, user, reply, tab)


@router.post("/{reply_id}/claim")
def claim_reply(request: Request, reply_id: int, tab: str = Form("needs_action"), db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    """"Take it": the reply (and its lead, if unowned) becomes yours, so teammates see it is being handled.
    HTMX (the reply box gets focus) gets a one-line note back; a plain form goes back to the reply."""
    reply = get_reply(db, reply_id, user)
    try:
        took = claim(db, reply, user)
    except ReplyTaken as exc:
        db.rollback()
        if request.headers.get("hx-request"):
            return render(request, "_claim_note.html", taken_by=exc.name)
        raise
    if took:
        audit(db, request, user, "reply_claimed", "reply", reply.id)
    db.commit()
    if request.headers.get("hx-request"):
        return render(request, "_claim_note.html", taken_by=None)
    flash(request, "It's yours. Teammates now see you are handling this reply.")
    return back(reply, tab)


@router.post("/{reply_id}/send")
def send_reply(request: Request, reply_id: int, body: str = Form(""), action: str = Form("send"),
               tab: str = Form("needs_action"), db: Session = Depends(get_db), user: User = Depends(current_user)):
    """FR-8.5: answer from the dashboard (or just keep the text as a draft)."""
    reply = get_reply(db, reply_id, user)
    claim(db, reply, user)
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
