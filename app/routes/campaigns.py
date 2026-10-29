from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.auth import require_role, verify_csrf
from app.db import get_db
from app.models import (
    DEFAULT_SCHEDULE, Campaign, CampaignSender, Enrollment, Event, Lead, Message, Reply, Sender, SequenceStep,
    Template, User,
    get_app_settings,
)
from app.services import campaigns as svc
from app.services.audit import audit
from app.services.events import health, since
from app.services.importer import parse_tags
from app.services.scheduler import mark_sent
from app.ui import flash, form_data, render

router = APIRouter(prefix="/campaigns", dependencies=[Depends(verify_csrf)])
managers = require_role("admin", "manager")

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EDITABLE = ("draft", "paused")


def get_campaign(db: Session, campaign_id: int) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    return campaign


def count(db: Session, stmt) -> int:
    return db.scalar(select(func.count()).select_from(stmt.subquery())) or 0


def sent_steps(db: Session, campaign: Campaign) -> set[int]:
    return set(db.scalars(select(Message.step_no).where(Message.campaign_id == campaign.id).distinct()))


def apply_form(db: Session, campaign: Campaign, form) -> str | None:
    """Copy the create/edit form onto the campaign and its steps. Returns an error or None."""
    name = str(form.get("name", "")).strip()
    if not name:
        return "Campaign name is required."
    if campaign.id is None or campaign.status == "draft":
        provider = form.get("provider", "")
        if provider not in ("ses", "smtp"):
            return "Choose SES or SMTP."
        campaign.provider = provider
    tags = parse_tags(str(form.get("tags", "")))
    if not tags:
        return "Add at least one lead tag for the segment."
    countries = [c.strip().upper() for c in str(form.get("countries", "")).split(",") if c.strip()]
    weekdays = [int(d) for d in form.getlist("weekdays") if str(d).isdigit() and 0 <= int(d) <= 6]
    schedule, problem = svc.validate_schedule(str(form.get("windows", "")), weekdays,
                                              str(form.get("default_tz", "")).strip() or "America/New_York")
    if problem:
        return problem
    daily_cap = str(form.get("daily_cap", "")).strip()
    if not daily_cap.isdigit() or not 1 <= int(daily_cap) <= 10_000:
        return "Daily cap must be between 1 and 10,000."
    start_at = None
    if start_date := str(form.get("start_date", "")).strip():
        try:
            day = datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            return "Start date must be a date."
        start_at = datetime.combine(day, time(0), tzinfo=ZoneInfo(schedule["default_tz"])).astimezone(timezone.utc)

    sender_ids = {int(i) for i in form.getlist("sender_ids") if str(i).isdigit()}
    senders = db.scalars(select(Sender).where(Sender.id.in_(sender_ids))).all() if sender_ids else []
    if not senders:
        return "Pick at least one sender."
    if any(s.type != campaign.provider for s in senders):
        return f"A {campaign.provider.upper()} campaign can only use {campaign.provider.upper()} senders."

    steps = []
    for n in range(1, svc.MAX_STEPS + 1):
        template_id = str(form.get(f"step_{n}_template", ""))
        if not template_id.isdigit():
            continue
        if db.get(Template, int(template_id)) is None:
            return f"Step {n}: template not found."
        delay = str(form.get(f"step_{n}_delay", "0")).strip() or "0"
        if not delay.isdigit() or (n > 1 and int(delay) < 1):
            return f"Step {n}: follow-ups need a delay of at least 1 business day."
        steps.append((n, int(template_id), 0 if n == 1 else int(delay), bool(form.get(f"step_{n}_same_thread"))))
    if not steps or steps[0][0] != 1:
        return "Step 1 (the initial email) needs a template."
    if [n for n, *_ in steps] != list(range(1, len(steps) + 1)):
        return "Steps must be filled in order without gaps."
    if campaign.id is not None:
        missing_sent = sent_steps(db, campaign) - {n for n, *_ in steps}
        if missing_sent:
            return f"Step(s) {sorted(missing_sent)} already sent emails and can't be removed."

    campaign.name, campaign.daily_cap, campaign.schedule, campaign.start_at = name, int(daily_cap), schedule, start_at
    campaign.segment = {"tags": tags, "countries": countries}
    campaign.stop_company_on_reply = bool(form.get("stop_company_on_reply"))
    campaign.allow_canada = bool(form.get("allow_canada"))
    db.add(campaign)
    db.flush()
    db.execute(delete(CampaignSender).where(CampaignSender.campaign_id == campaign.id))
    db.add_all(CampaignSender(campaign_id=campaign.id, sender_id=s.id) for s in senders)
    existing = {s.step_no: s for s in svc.steps_of(db, campaign)}
    for n, template_id, delay, same_thread in steps:
        step = existing.pop(n, None) or SequenceStep(campaign_id=campaign.id, step_no=n)
        step.template_id, step.delay_business_days, step.same_thread = template_id, delay, same_thread
        db.add(step)
    for step in existing.values():
        db.delete(step)
    return None


def form_context(db: Session, campaign: Campaign | None) -> dict:
    steps = {s.step_no: s for s in svc.steps_of(db, campaign)} if campaign and campaign.id else {}
    schedule = campaign.schedule if campaign and campaign.schedule else DEFAULT_SCHEDULE
    return {
        "c": campaign,
        "steps": steps,
        "templates": db.scalars(select(Template).order_by(Template.name)).all(),
        "senders": db.scalars(select(Sender).where(Sender.status != "disabled").order_by(Sender.type, Sender.from_email)).all(),
        "pool_ids": {s.id for s in svc.pool_of(db, campaign)} if campaign and campaign.id else set(),
        "schedule": schedule,
        "windows": ", ".join(f"{a}-{b}" for a, b in schedule["windows"]),
        "weekdays": WEEKDAYS,
        "max_steps": svc.MAX_STEPS,
        "locked_steps": sent_steps(db, campaign) if campaign and campaign.id else set(),
    }


@router.get("")
def list_campaigns(request: Request, db: Session = Depends(get_db), user: User = Depends(managers)):
    campaigns = db.scalars(select(Campaign).order_by(Campaign.id.desc())).all()
    stats = {}
    for c in campaigns:
        stats[c.id] = {
            "enrolled": count(db, select(Enrollment.id).where(Enrollment.campaign_id == c.id)),
            "sent": count(db, select(Message.id).where(Message.campaign_id == c.id, Message.status == "sent")),
            "next": db.scalar(select(func.min(Enrollment.next_send_at)).where(
                Enrollment.campaign_id == c.id, Enrollment.state == "active")),
        }
    return render(request, "campaigns/list.html", campaigns=campaigns, stats=stats)


@router.get("/new")
def new_campaign(request: Request, db: Session = Depends(get_db), user: User = Depends(managers)):
    return render(request, "campaigns/edit.html", **form_context(db, None))


@router.post("")
def create_campaign(request: Request, form=Depends(form_data), db: Session = Depends(get_db),
                    user: User = Depends(managers)):
    campaign = Campaign(status="draft", created_by=user.id, provider=form.get("provider", ""))
    problem = apply_form(db, campaign, form)
    if problem:
        db.rollback()
        flash(request, problem, "error")
        return RedirectResponse("/campaigns/new", status_code=303)
    audit(db, request, user, "campaign_created", "campaign", campaign.id, name=campaign.name)
    db.commit()
    flash(request, "Campaign saved as draft. Check the pre-launch list, then start it.")
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


@router.get("/{campaign_id}/edit")
def edit_campaign(request: Request, campaign_id: int, db: Session = Depends(get_db), user: User = Depends(managers)):
    campaign = get_campaign(db, campaign_id)
    if campaign.status not in EDITABLE:
        flash(request, "Pause the campaign before editing it.", "error")
        return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)
    return render(request, "campaigns/edit.html", **form_context(db, campaign))


@router.post("/{campaign_id}/edit")
def save_campaign(request: Request, campaign_id: int, form=Depends(form_data), db: Session = Depends(get_db),
                  user: User = Depends(managers)):
    campaign = get_campaign(db, campaign_id)
    if campaign.status not in EDITABLE:
        raise HTTPException(status_code=400, detail="Only draft or paused campaigns can be edited.")
    problem = apply_form(db, campaign, form)
    if problem:
        db.rollback()
        flash(request, problem, "error")
        return RedirectResponse(f"/campaigns/{campaign.id}/edit", status_code=303)
    audit(db, request, user, "campaign_updated", "campaign", campaign.id)
    db.commit()
    flash(request, "Campaign saved.")
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


@router.get("/{campaign_id}")
def campaign_detail(request: Request, campaign_id: int, db: Session = Depends(get_db), user: User = Depends(managers)):
    c = get_campaign(db, campaign_id)
    steps = svc.steps_of(db, c)
    by_step = {}
    for step_no, status, variant, n in db.execute(
            select(Message.step_no, Message.status, Message.variant, func.count())
            .where(Message.campaign_id == c.id).group_by(Message.step_no, Message.status, Message.variant)):
        s = by_step.setdefault(step_no, {"sent": 0, "failed": 0, "pending": 0, "variants": {}})
        if status == "sent":
            s["sent"] += n
            s["variants"][variant] = s["variants"].get(variant, 0) + n
        elif status == "failed":
            s["failed"] += n
        else:
            s["pending"] += n
    events = {}
    for step_no, type_, n in db.execute(
            select(Message.step_no, Event.type, func.count(func.distinct(Event.message_id)))
            .join(Message, Message.id == Event.message_id)
            .where(Event.campaign_id == c.id).group_by(Message.step_no, Event.type)):
        events.setdefault(step_no, {})[type_] = n
    totals = {t: sum(e.get(t, 0) for e in events.values()) for t in ("delivery", "bounce_hard", "complaint")}
    replies = {}
    for step_no, outcome, n in db.execute(
            select(Message.step_no, Reply.outcome, func.count(func.distinct(Reply.lead_id)))
            .join(Message, Message.id == Reply.message_id)
            .where(Message.campaign_id == c.id, Reply.direction == "in", Reply.classification != "auto_reply")
            .group_by(Message.step_no, Reply.outcome)):
        r = replies.setdefault(step_no, {"all": 0, "positive": 0})
        r["all"] += n
        r["positive"] += n if outcome in ("interested", "meeting_booked") else 0
    totals["replies"] = sum(r["all"] for r in replies.values())
    totals["positive"] = sum(r["positive"] for r in replies.values())
    variant_replies = dict(db.execute(
        select(Message.variant, func.count(func.distinct(Reply.lead_id))).join(Message, Message.id == Reply.message_id)
        .where(Message.campaign_id == c.id, Reply.direction == "in", Reply.classification != "auto_reply").group_by(Message.variant)).all())
    enrolled = count(db, select(Enrollment.id).where(Enrollment.campaign_id == c.id))
    stopped = dict(db.execute(select(Enrollment.stop_reason, func.count()).where(
        Enrollment.campaign_id == c.id, Enrollment.state == "stopped").group_by(Enrollment.stop_reason)).all())
    states = dict(db.execute(select(Enrollment.state, func.count()).where(Enrollment.campaign_id == c.id)
                             .group_by(Enrollment.state)).all())
    sent_total = sum(s["sent"] for s in by_step.values())
    unknown = db.scalars(select(Message).where(Message.campaign_id == c.id, Message.status == "unknown")).all()
    recent = db.execute(select(Message, Lead.email).join(Lead, Lead.id == Message.lead_id)
                        .where(Message.campaign_id == c.id).order_by(Message.id.desc()).limit(20)).all()
    next_send = db.scalar(select(func.min(Enrollment.next_send_at)).where(
        Enrollment.campaign_id == c.id, Enrollment.state == "active"))
    return render(
        request, "campaigns/detail.html", c=c, steps=steps, by_step=by_step,
        templates={t.id: t for t in db.scalars(select(Template)).all()},
        pool=svc.pool_of(db, c), checks=svc.preflight(db, c), enrolled=enrolled, sent_total=sent_total,
        stopped=stopped, states=states, unknown=unknown, recent=recent, next_send=next_send, weekdays=WEEKDAYS,
        domain_max=get_app_settings(db).domain_daily_max, events=events, totals=totals, replies=replies,
        variant_replies=variant_replies,
        health=health(db, datetime.now(timezone.utc), Message.campaign_id == c.id, *since(c.health_since)),
        summary=svc.segment_summary(db, c) if c.status in ("draft", "paused", "running", "scheduled") else None,
    )


def change(request: Request, db: Session, user: User, c: Campaign, action: str, message: str):
    audit(db, request, user, f"campaign_{action}", "campaign", c.id, status=c.status)
    db.commit()
    flash(request, message)
    return RedirectResponse(f"/campaigns/{c.id}", status_code=303)


@router.post("/{campaign_id}/start")
def start_campaign(request: Request, campaign_id: int, db: Session = Depends(get_db), user: User = Depends(managers)):
    c = get_campaign(db, campaign_id)
    if c.status != "draft":
        raise HTTPException(status_code=400, detail="Only a draft campaign can be started.")
    failed = [label for label, ok, _ in svc.preflight(db, c) if not ok]
    if failed:
        flash(request, "Fix the pre-launch checklist first: " + ", ".join(failed), "error")
        return RedirectResponse(f"/campaigns/{c.id}", status_code=303)
    added = svc.start(db, c)
    return change(request, db, user, c, "started", f"Campaign {c.status}: {added:,} leads enrolled.")


@router.post("/{campaign_id}/pause")
def pause_campaign(request: Request, campaign_id: int, db: Session = Depends(get_db), user: User = Depends(managers)):
    c = get_campaign(db, campaign_id)
    if c.status not in ("running", "scheduled"):
        raise HTTPException(status_code=400, detail="Campaign is not running.")
    c.status, c.pause_reason = "paused", "Paused by " + user.name
    return change(request, db, user, c, "paused", "Campaign paused. No more emails go out (takes effect within seconds).")


@router.post("/{campaign_id}/resume")
def resume_campaign(request: Request, campaign_id: int, db: Session = Depends(get_db), user: User = Depends(managers)):
    c = get_campaign(db, campaign_id)
    if c.status != "paused":
        raise HTTPException(status_code=400, detail="Campaign is not paused.")
    failed = [label for label, ok, _ in svc.preflight(db, c) if not ok]
    if failed:
        flash(request, "Fix the pre-launch checklist first: " + ", ".join(failed), "error")
        return RedirectResponse(f"/campaigns/{c.id}", status_code=303)
    c.status, c.pause_reason = "running", ""
    c.health_since = datetime.now(timezone.utc)  # auto-pause judges only sends from now on
    return change(request, db, user, c, "resumed", "Campaign resumed.")


@router.post("/{campaign_id}/stop")
def stop_campaign(request: Request, campaign_id: int, db: Session = Depends(get_db), user: User = Depends(managers)):
    c = get_campaign(db, campaign_id)
    if c.status in ("completed", "stopped"):
        raise HTTPException(status_code=400, detail="Campaign already ended.")
    svc.stop(db, c)
    return change(request, db, user, c, "stopped", "Campaign stopped. All its leads left the sequence.")


@router.post("/{campaign_id}/enroll")
def enroll_more(request: Request, campaign_id: int, db: Session = Depends(get_db), user: User = Depends(managers)):
    c = get_campaign(db, campaign_id)
    if c.status not in ("running", "scheduled", "paused"):
        raise HTTPException(status_code=400, detail="Start the campaign first.")
    added = svc.enroll(db, c, datetime.now(timezone.utc))
    return change(request, db, user, c, "enrolled_more", f"{added:,} new matching leads added.")


@router.post("/{campaign_id}/messages/{message_id}/resolve")
def resolve_unknown(request: Request, campaign_id: int, message_id: int, form=Depends(form_data),
                    db: Session = Depends(get_db), user: User = Depends(managers)):
    """A send with unknown result: a human says whether it went out (check the sender's Sent folder)."""
    message = db.get(Message, message_id)
    if message is None or message.campaign_id != campaign_id or message.status != "unknown":
        raise HTTPException(status_code=404, detail="Message not found.")
    c = get_campaign(db, campaign_id)
    if form.get("outcome") == "sent":
        mark_sent(db, message, message.provider_message_id or message.message_id_header, message.created_at)
        text = "Marked as sent; the sequence continues."
    else:
        enr = db.get(Enrollment, message.enrollment_id)
        db.delete(message)
        if enr.state == "active":
            enr.next_send_at = datetime.now(timezone.utc)
        text = "Will be sent again at the next send window."
    return change(request, db, user, c, "unknown_resolved", text)
