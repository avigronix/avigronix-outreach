from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role, verify_csrf
from app.db import get_db
from app.models import Lead, Sender, SequenceStep, Template, User, get_app_settings
from app.services.audit import audit
from app.services.crypto import unsubscribe_url
from app.services.providers import SendError, provider_for
from app.services.render import SAMPLE_LEAD, compose, lead_values, lint, render_content
from app.services.suppression import parse_value
from app.ui import flash, render

router = APIRouter(prefix="/templates", dependencies=[Depends(verify_csrf)])
managers = require_role("admin", "manager")


def get_template(db: Session, template_id: int) -> Template:
    t = db.get(Template, template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found.")
    return t


def clean_subjects(*subjects: str) -> list[str]:
    return [" ".join(s.split()) for s in subjects if s and s.strip()][:3]


def preview_lead(db: Session, lead_id: str):
    lead = db.get(Lead, int(lead_id)) if lead_id.isdigit() else None
    return lead if lead is not None else SAMPLE_LEAD


def build_preview(db: Session, subjects: list[str], body_text: str, body_html: str, lead_id: str) -> dict:
    s = get_app_settings(db)
    lead = preview_lead(db, lead_id)
    values = lead_values(lead, "Vikas Yadav")
    email = lead.email if isinstance(lead, Lead) else lead["email"]
    previews = [
        render_content(subj, body_text, body_html, values, s.company_name, s.postal_address, unsubscribe_url(email))
        for subj in (subjects or [""])
    ]
    return {"previews": previews, "warnings": lint(subjects, body_text, body_html),
            "missing": sorted({m for p in previews for m in p["missing"]}),
            "no_address": not s.postal_address.strip()}


def editor_context(db: Session, t: Template | None, lead_id: str = "") -> dict:
    subjects = t.subject_variants if t else []
    return {
        "t": t,
        "subjects": subjects + [""] * (3 - len(subjects)),
        "leads": db.scalars(select(Lead).order_by(Lead.id.desc()).limit(100)).all(),
        "senders": db.scalars(select(Sender).where(Sender.status != "disabled").order_by(Sender.from_email)).all(),
        "lead_id": lead_id,
        **build_preview(db, subjects, t.body_text if t else "", t.body_html if t else "", lead_id),
    }


@router.get("")
def list_templates(request: Request, db: Session = Depends(get_db), user: User = Depends(managers)):
    templates = db.scalars(select(Template).order_by(Template.name)).all()
    return render(request, "templates/list.html", templates=templates)


@router.get("/new")
def new_template(request: Request, db: Session = Depends(get_db), user: User = Depends(managers)):
    return render(request, "templates/edit.html", **editor_context(db, None))


@router.post("")
def create_template(
    request: Request,
    name: str = Form(...),
    subject_a: str = Form(...),
    subject_b: str = Form(""),
    subject_c: str = Form(""),
    body_text: str = Form(""),
    body_html: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(managers),
):
    subjects = clean_subjects(subject_a, subject_b, subject_c)
    if not name.strip() or not subjects:
        flash(request, "Name and at least one subject are required.", "error")
        return RedirectResponse("/templates/new", status_code=303)
    t = Template(name=name.strip(), subject_variants=subjects, body_text=body_text.strip(),
                 body_html=body_html.strip(), created_by=user.id)
    db.add(t)
    db.flush()
    audit(db, request, user, "template_created", "template", t.id, name=t.name)
    db.commit()
    flash(request, "Template saved.")
    return RedirectResponse(f"/templates/{t.id}", status_code=303)


@router.post("/preview")
def live_preview(
    request: Request,
    subject_a: str = Form(""),
    subject_b: str = Form(""),
    subject_c: str = Form(""),
    body_text: str = Form(""),
    body_html: str = Form(""),
    lead_id: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(managers),
):
    """HTMX: re-render the preview panel from the editor's current (unsaved) fields."""
    subjects = clean_subjects(subject_a, subject_b, subject_c)
    return render(request, "templates/_preview.html", **build_preview(db, subjects, body_text, body_html, lead_id))


@router.get("/{template_id}")
def edit_template(request: Request, template_id: int, lead_id: str = "", db: Session = Depends(get_db),
                  user: User = Depends(managers)):
    return render(request, "templates/edit.html", **editor_context(db, get_template(db, template_id), lead_id))


@router.post("/{template_id}")
def save_template(
    request: Request,
    template_id: int,
    name: str = Form(...),
    subject_a: str = Form(...),
    subject_b: str = Form(""),
    subject_c: str = Form(""),
    body_text: str = Form(""),
    body_html: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(managers),
):
    t = get_template(db, template_id)
    subjects = clean_subjects(subject_a, subject_b, subject_c)
    if not name.strip() or not subjects:
        flash(request, "Name and at least one subject are required.", "error")
        return RedirectResponse(f"/templates/{t.id}", status_code=303)
    t.name, t.subject_variants = name.strip(), subjects
    t.body_text, t.body_html = body_text.strip(), body_html.strip()
    audit(db, request, user, "template_updated", "template", t.id)
    db.commit()
    flash(request, "Template saved. Emails already sent keep their old text.")
    return RedirectResponse(f"/templates/{t.id}", status_code=303)


@router.post("/{template_id}/test")
def send_test(
    request: Request,
    template_id: int,
    sender_id: int = Form(...),
    to_email: str = Form(...),
    lead_id: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(managers),
):
    """Send every subject variant of the saved template to an internal address."""
    t = get_template(db, template_id)
    sender = db.get(Sender, sender_id)
    parsed = parse_value(to_email)
    back = RedirectResponse(f"/templates/{t.id}?lead_id={lead_id}", status_code=303)
    if sender is None or parsed is None or parsed[1] != "email":
        flash(request, "Choose a sender and a valid test address.", "error")
        return back
    s = get_app_settings(db)
    values = lead_values(preview_lead(db, lead_id), sender.from_name)
    try:
        for i, subject in enumerate(t.subject_variants):
            msg, _ = compose(sender, parsed[0], f"[TEST {'ABC'[i]}] {subject}", t.body_text, t.body_html, values,
                             s.company_name, s.postal_address, unsubscribe_url(parsed[0]))
            provider_for(sender).send(msg)
    except (SendError, RuntimeError) as exc:
        flash(request, f"Test email failed: {exc}", "error")
        return back
    t.test_sent_at = datetime.now(timezone.utc)
    audit(db, request, user, "template_test_sent", "template", t.id, to=parsed[0], sender=sender.from_email)
    db.commit()
    flash(request, f"Sent {len(t.subject_variants)} test email(s) to {parsed[0]} from {sender.from_email}.")
    return back


@router.post("/{template_id}/delete")
def delete_template(request: Request, template_id: int, db: Session = Depends(get_db), user: User = Depends(managers)):
    t = get_template(db, template_id)
    if db.scalar(select(SequenceStep.id).where(SequenceStep.template_id == t.id).limit(1)):
        flash(request, "This template is used in a campaign sequence and can't be deleted.", "error")
        return RedirectResponse(f"/templates/{t.id}", status_code=303)
    audit(db, request, user, "template_deleted", "template", t.id, name=t.name)
    db.delete(t)
    db.commit()
    flash(request, f"Template {t.name} deleted.")
    return RedirectResponse("/templates", status_code=303)
