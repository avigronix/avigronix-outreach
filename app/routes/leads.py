import csv
import io
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.auth import current_user, require_role, verify_csrf
from app.db import get_db
from app.models import LEAD_STATUSES, SOURCE_TYPES, Campaign, Enrollment, Event, Lead, LeadImport, Message, User
from app.services.audit import audit
from app.services.crypto import unsubscribe_url
from app.services.importer import parse_tags
from app.services.suppression import stop_lead, suppress
from app.ui import flash, form_data, render

router = APIRouter(prefix="/leads", dependencies=[Depends(verify_csrf)])
managers = require_role("admin", "manager")
admin_only = require_role("admin")

PAGE_SIZE = 50
FILTERS = ("q", "status", "tag", "source", "country", "state", "import_id", "owner", "campaign")
EXPORT_COLUMNS = ["email", "first_name", "last_name", "company", "title", "website", "city", "state",
                  "country", "timezone", "industry", "status", "source_type", "tags", "notes"]


def read_filters(source) -> dict[str, str]:
    return {k: str(source.get(k, "")).strip() for k in FILTERS if str(source.get(k, "")).strip()}


def filtered_leads(filters: dict[str, str], user: User):
    """SELECT for the lead list, export and 'apply to all matching'. Sales only see their own leads."""
    stmt = select(Lead)
    if user.role == "sales":
        stmt = stmt.where(Lead.owner_id == user.id)
    if q := filters.get("q"):
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(
            Lead.email.like(like),
            func.lower(Lead.company).like(like),
            func.lower(Lead.first_name + " " + Lead.last_name).like(like),
        ))
    if filters.get("status") in LEAD_STATUSES:
        stmt = stmt.where(Lead.status == filters["status"])
    if tag := filters.get("tag"):
        stmt = stmt.where(Lead.tags.any(tag.lower()))
    if filters.get("source") in SOURCE_TYPES:
        stmt = stmt.where(Lead.source_type == filters["source"])
    if country := filters.get("country"):
        stmt = stmt.where(func.upper(Lead.country) == country.upper())
    if state := filters.get("state"):
        stmt = stmt.where(func.upper(Lead.state) == state.upper())
    if filters.get("import_id", "").isdigit():
        stmt = stmt.where(Lead.import_id == int(filters["import_id"]))
    if filters.get("campaign", "").isdigit():
        stmt = stmt.where(Lead.id.in_(select(Enrollment.lead_id).where(Enrollment.campaign_id == int(filters["campaign"]))))
    if filters.get("owner") == "none":
        stmt = stmt.where(Lead.owner_id.is_(None))
    elif filters.get("owner", "").isdigit():
        stmt = stmt.where(Lead.owner_id == int(filters["owner"]))
    return stmt


def get_lead(db: Session, lead_id: int, user: User) -> Lead:
    lead = db.get(Lead, lead_id)
    if lead is None or (user.role == "sales" and lead.owner_id != user.id):
        raise HTTPException(status_code=404, detail="Lead not found.")
    return lead


def active_users(db: Session) -> list[User]:
    return db.scalars(select(User).where(User.is_active).order_by(User.name)).all()


@router.get("")
def list_leads(request: Request, page: int = 1, db: Session = Depends(get_db), user: User = Depends(current_user)):
    filters = read_filters(request.query_params)
    stmt = filtered_leads(filters, user)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    page = max(1, min(page, (total + PAGE_SIZE - 1) // PAGE_SIZE or 1))
    leads = db.scalars(stmt.order_by(Lead.id.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    owners = {u.id: u.name for u in active_users(db)}
    return render(
        request, "leads/list.html", leads=leads, total=total, page=page, page_size=PAGE_SIZE,
        filters=filters, query=urlencode(filters), statuses=LEAD_STATUSES, source_types=SOURCE_TYPES,
        owners=owners, can_manage=user.role in ("admin", "manager"),
        campaigns=db.execute(select(Campaign.id, Campaign.name).order_by(Campaign.id.desc())).all(),
    )


@router.get("/export.csv")
def export_csv(request: Request, db: Session = Depends(get_db), user: User = Depends(managers)):
    filters = read_filters(request.query_params)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(EXPORT_COLUMNS + ["custom"])
    for lead in db.scalars(filtered_leads(filters, user).order_by(Lead.id)).yield_per(1000):
        values = [getattr(lead, c) for c in EXPORT_COLUMNS]
        values[EXPORT_COLUMNS.index("tags")] = ",".join(lead.tags)
        custom = "; ".join(f"{k}={v}" for k, v in lead.custom.items())
        writer.writerow([v if v is not None else "" for v in values] + [custom])
    audit(db, request, user, "leads_exported", filters=filters)
    db.commit()
    return Response(
        "﻿" + out.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="leads.csv"'},
    )


@router.post("/bulk")
def bulk_action(request: Request, form=Depends(form_data), db: Session = Depends(get_db), user: User = Depends(managers)):
    action = form.get("action", "")
    value = str(form.get("value", "")).strip()
    filters = read_filters(form)
    back = RedirectResponse(f"/leads?{urlencode(filters)}", status_code=303)

    if form.get("all_matching"):
        ids = db.scalars(filtered_leads(filters, user).with_only_columns(Lead.id)).all()
    else:
        ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
    if not ids:
        flash(request, "Select at least one lead.", "error")
        return back

    if action in ("add_tag", "remove_tag"):
        tags = parse_tags(value)
        if not tags:
            flash(request, "Type a tag.", "error")
            return back
        for i in range(0, len(ids), 1000):
            for lead in db.scalars(select(Lead).where(Lead.id.in_(ids[i : i + 1000]))):
                if action == "add_tag":
                    lead.tags = sorted(set(lead.tags) | set(tags))
                else:
                    lead.tags = [t for t in lead.tags if t not in tags]
        message = f"Tags updated on {len(ids)} leads."
    elif action == "set_owner":
        owner_id = int(value) if value.isdigit() else None
        if owner_id is not None and db.get(User, owner_id) is None:
            raise HTTPException(status_code=400, detail="Unknown user.")
        for i in range(0, len(ids), 1000):
            for lead in db.scalars(select(Lead).where(Lead.id.in_(ids[i : i + 1000]))):
                lead.owner_id = owner_id
        message = f"Owner set on {len(ids)} leads."
    elif action == "delete":
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Only an admin can delete leads.")
        for i in range(0, len(ids), 1000):
            db.execute(delete(Lead).where(Lead.id.in_(ids[i : i + 1000])))
        message = f"Deleted {len(ids)} leads."
    else:
        raise HTTPException(status_code=400, detail="Unknown action.")

    audit(db, request, user, f"leads_bulk_{action}", "lead", None, count=len(ids), value=value)
    db.commit()
    flash(request, message)
    return back


@router.get("/{lead_id}")
def lead_detail(request: Request, lead_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    lead = get_lead(db, lead_id, user)
    imp = db.get(LeadImport, lead.import_id) if lead.import_id else None
    enrollments = db.execute(select(Enrollment, Campaign.name).join(Campaign, Campaign.id == Enrollment.campaign_id)
                             .where(Enrollment.lead_id == lead.id).order_by(Enrollment.id.desc())).all()
    messages = db.execute(select(Message, Campaign.name).join(Campaign, Campaign.id == Message.campaign_id)
                          .where(Message.lead_id == lead.id).order_by(Message.id.desc())).all()
    return render(
        request, "leads/detail.html", lead=lead, imp=imp, statuses=LEAD_STATUSES, source_types=SOURCE_TYPES,
        enrollments=enrollments, messages=messages,
        events=db.scalars(select(Event).where(Event.lead_id == lead.id).order_by(Event.occurred_at.desc())).all(),
        users=active_users(db), can_manage=user.role in ("admin", "manager"),
        unsubscribe_link=unsubscribe_url(lead.email) if user.role == "admin" else None,
    )


@router.post("/{lead_id}")
def update_lead(
    request: Request,
    lead_id: int,
    status: str = Form(...),
    tags: str = Form(""),
    owner_id: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(managers),
):
    lead = get_lead(db, lead_id, user)
    if status not in LEAD_STATUSES:
        raise HTTPException(status_code=400, detail="Unknown status.")
    changes = {}
    if status != lead.status:
        changes["status"] = [lead.status, status]
        lead.status = status
    lead.tags = parse_tags(tags)
    lead.owner_id = int(owner_id) if owner_id.isdigit() else None
    audit(db, request, user, "lead_updated", "lead", lead.id, **changes)
    db.commit()
    flash(request, "Lead saved.")
    return RedirectResponse(f"/leads/{lead.id}", status_code=303)


@router.post("/{lead_id}/notes")
def save_notes(
    request: Request,
    lead_id: int,
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    lead = get_lead(db, lead_id, user)
    lead.notes = notes.strip()
    db.commit()
    flash(request, "Notes saved.")
    return RedirectResponse(f"/leads/{lead.id}", status_code=303)


@router.post("/{lead_id}/stop")
def stop_sequence(request: Request, lead_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Manual stop (stop rule 5): the lead leaves its active campaign."""
    lead = get_lead(db, lead_id, user)
    stop_lead(db, lead.id, "manual")
    audit(db, request, user, "lead_sequence_stopped", "lead", lead.id)
    db.commit()
    flash(request, "Sequence stopped for this lead.")
    return RedirectResponse(f"/leads/{lead.id}", status_code=303)


@router.post("/{lead_id}/suppress")
def suppress_lead(request: Request, lead_id: int, db: Session = Depends(get_db), user: User = Depends(managers)):
    lead = get_lead(db, lead_id, user)
    suppress(db, lead.email, "email", "manual", f"lead #{lead.id}", user.id)
    audit(db, request, user, "suppression_added", "lead", lead.id, value=lead.email)
    db.commit()
    flash(request, f"{lead.email} added to the suppression list.")
    return RedirectResponse(f"/leads/{lead.id}", status_code=303)


@router.post("/{lead_id}/erase")
def erase_lead(request: Request, lead_id: int, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    """Data deletion request (GDPR/DPDP): delete the lead but keep the email suppressed,
    so a later import can't bring it back."""
    lead = get_lead(db, lead_id, user)
    suppress(db, lead.email, "email", "erasure", "deletion request", user.id)
    audit(db, request, user, "lead_erased", "lead", lead.id)
    db.delete(lead)
    db.commit()
    flash(request, "Lead deleted. The email stays on the suppression list.")
    return RedirectResponse("/leads", status_code=303)
