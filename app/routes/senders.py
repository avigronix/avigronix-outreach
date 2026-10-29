from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_role, verify_csrf
from app.config import settings
from app.db import get_db
from app.models import Sender, User, get_app_settings
from app.services.audit import audit
from app.services.crypto import encrypt_secret, unsubscribe_url
from app.services.dns_check import check_sender_dns
from app.services.providers import SendError, provider_for
from app.services.render import compose
from app.services.suppression import parse_value
from app.services.validation import normalize_email
from app.services.warmup import DEFAULTS, WARMUP_DAYS, effective_daily_cap, warmup_cap
from app.ui import flash, form_data, render

router = APIRouter(prefix="/senders", dependencies=[Depends(verify_csrf)])
admin_only = require_role("admin")

TEST_BODY = (
    "This is a test from the Avigronix outreach dashboard.\n\n"
    "If you can read this, the sender {email} is set up correctly. "
    "Check 'Show original' in Gmail for SPF, DKIM and DMARC: all three should say PASS."
)


def get_sender(db: Session, sender_id: int) -> Sender:
    sender = db.get(Sender, sender_id)
    if sender is None:
        raise HTTPException(status_code=404, detail="Sender not found.")
    return sender


def as_int(value, default: int | None = None) -> int | None:
    value = str(value or "").strip()
    return int(value) if value.lstrip("-").isdigit() else default


def apply_form(db: Session, sender: Sender, form) -> str | None:
    """Copy form fields onto the sender. Returns an error message, or None if valid."""
    from_email = normalize_email(str(form.get("from_email", "")))
    if not from_email:
        return "From email is not valid."
    clash = db.scalar(select(Sender.id).where(func.lower(Sender.from_email) == from_email, Sender.id != sender.id))
    if clash:
        return f"A sender with {from_email} already exists."
    from_name = str(form.get("from_name", "")).strip()
    if not from_name:
        return "From name is required (a person works best, e.g. 'Vikas from Avigronix')."
    reply_to = normalize_email(str(form.get("reply_to", ""))) or (
        settings.reply_email if sender.type == "ses" and settings.reply_email else from_email)

    default_cap, default_min, default_max = DEFAULTS[sender.type]
    daily_cap = as_int(form.get("daily_cap"), default_cap)
    gap_min = as_int(form.get("gap_min_sec"), default_min)
    gap_max = as_int(form.get("gap_max_sec"), default_max)
    if not 1 <= daily_cap <= 10_000:
        return "Daily cap must be between 1 and 10,000."
    if gap_min < 0 or gap_max < gap_min:
        return "Gap between sends: minimum must be ≥ 0 and not more than the maximum."

    if sender.type == "smtp":
        host = str(form.get("smtp_host", "")).strip()
        port = as_int(form.get("smtp_port"), 465)
        password = str(form.get("smtp_password", ""))
        if not host:
            return "SMTP host is required (Zoho: smtp.zoho.com, Google: smtp.gmail.com)."
        if port not in (465, 587):
            return "SMTP port must be 465 (SSL) or 587 (STARTTLS)."
        if not password and not sender.smtp_password_enc:
            return "SMTP password (app password) is required."
        sender.smtp_host, sender.smtp_port = host, port
        sender.smtp_user = str(form.get("smtp_user", "")).strip() or from_email
        if password:
            sender.smtp_password_enc = encrypt_secret(password)
        sender.imap_host = str(form.get("imap_host", "")).strip()
        sender.imap_port = as_int(form.get("imap_port"), 993)
        sender.dkim_selector = str(form.get("dkim_selector", "")).strip()

    sender.from_email, sender.from_name, sender.reply_to = from_email, from_name, reply_to
    sender.daily_cap, sender.gap_min_sec, sender.gap_max_sec = daily_cap, gap_min, gap_max
    sender.warmup_day = max(1, as_int(form.get("warmup_day"), sender.warmup_day or 1))
    override = as_int(form.get("warmup_override_cap"))
    sender.warmup_override_cap = override if override and override > 0 else None
    return None


@router.get("")
def list_senders(request: Request, db: Session = Depends(get_db), admin: User = Depends(admin_only)):
    senders = db.scalars(select(Sender).order_by(Sender.type, Sender.from_email)).all()
    return render(request, "senders/list.html", senders=senders, cap=effective_daily_cap, warmup_days=WARMUP_DAYS)


@router.get("/new")
def new_sender(request: Request, type: str = "smtp", admin: User = Depends(admin_only)):
    if type not in DEFAULTS:
        raise HTTPException(status_code=400, detail="Unknown sender type.")
    cap, gap_min, gap_max = DEFAULTS[type]
    draft = Sender(type=type, daily_cap=cap, gap_min_sec=gap_min, gap_max_sec=gap_max, warmup_day=1,
                   smtp_port=465, imap_port=993, from_name="", from_email="",
                   reply_to=settings.reply_email if type == "ses" else "")
    return render(request, "senders/edit.html", s=draft, is_new=True, ses_ready=bool(settings.aws_access_key_id))


@router.post("")
def create_sender(request: Request, form=Depends(form_data), db: Session = Depends(get_db),
                  admin: User = Depends(admin_only)):
    sender_type = form.get("type", "")
    if sender_type not in DEFAULTS:
        raise HTTPException(status_code=400, detail="Unknown sender type.")
    sender = Sender(type=sender_type, status="active", warmup_day=1)
    try:
        problem = apply_form(db, sender, form)
    except RuntimeError as exc:  # FERNET_KEY missing
        problem = str(exc)
    if problem:
        flash(request, problem, "error")
        return RedirectResponse(f"/senders/new?type={sender_type}", status_code=303)
    db.add(sender)
    db.flush()
    audit(db, request, admin, "sender_created", "sender", sender.id, email=sender.from_email, type=sender.type)
    db.commit()
    flash(request, f"Sender {sender.from_email} added. Run the DNS check and send a test next.")
    return RedirectResponse(f"/senders/{sender.id}", status_code=303)


@router.get("/{sender_id}")
def edit_sender(request: Request, sender_id: int, db: Session = Depends(get_db), admin: User = Depends(admin_only)):
    sender = get_sender(db, sender_id)
    return render(
        request, "senders/edit.html", s=sender, is_new=False, ses_ready=bool(settings.aws_access_key_id),
        cap=effective_daily_cap(sender), table_cap=warmup_cap(sender.type, sender.warmup_day),
        warmup_days=WARMUP_DAYS,
    )


@router.post("/{sender_id}")
def save_sender(request: Request, sender_id: int, form=Depends(form_data), db: Session = Depends(get_db),
                admin: User = Depends(admin_only)):
    sender = get_sender(db, sender_id)
    before = {"daily_cap": sender.daily_cap, "warmup_day": sender.warmup_day,
              "warmup_override_cap": sender.warmup_override_cap, "from_email": sender.from_email}
    try:
        problem = apply_form(db, sender, form)
    except RuntimeError as exc:
        problem = str(exc)
    if problem:
        db.rollback()
        flash(request, problem, "error")
        return RedirectResponse(f"/senders/{sender.id}", status_code=303)
    after = {k: getattr(sender, k) for k in before}
    audit(db, request, admin, "sender_updated", "sender", sender.id,
          changes={k: [before[k], after[k]] for k in before if before[k] != after[k]})
    db.commit()
    flash(request, "Sender saved.")
    return RedirectResponse(f"/senders/{sender.id}", status_code=303)


@router.post("/{sender_id}/dns-check")
def dns_check(request: Request, sender_id: int, db: Session = Depends(get_db), admin: User = Depends(admin_only)):
    sender = get_sender(db, sender_id)
    sender.dns_result = check_sender_dns(sender)
    sender.dns_ok = all(ok for ok, _ in sender.dns_result.values())
    sender.dns_checked_at = datetime.now(timezone.utc)
    db.commit()
    flash(request, "DNS check passed." if sender.dns_ok else "DNS check found problems — see below.",
          "ok" if sender.dns_ok else "error")
    return RedirectResponse(f"/senders/{sender.id}", status_code=303)


@router.post("/{sender_id}/test")
def send_test(request: Request, sender_id: int, to_email: str = Form(...), db: Session = Depends(get_db),
              admin: User = Depends(admin_only)):
    sender = get_sender(db, sender_id)
    parsed = parse_value(to_email)
    if parsed is None or parsed[1] != "email":
        flash(request, "Enter a valid test address.", "error")
        return RedirectResponse(f"/senders/{sender.id}", status_code=303)
    s = get_app_settings(db)
    msg, _ = compose(sender, parsed[0], "Test from Avigronix outreach dashboard",
                     TEST_BODY.format(email=sender.from_email), "", {}, s.company_name, s.postal_address,
                     unsubscribe_url(parsed[0]))
    try:
        provider_id = provider_for(sender).send(msg)
    except (SendError, RuntimeError) as exc:
        flash(request, f"Test failed: {exc}", "error")
        return RedirectResponse(f"/senders/{sender.id}", status_code=303)
    audit(db, request, admin, "sender_test_sent", "sender", sender.id, to=parsed[0], provider_id=provider_id)
    db.commit()
    flash(request, f"Test email sent to {parsed[0]}. Check the inbox (and spam folder).")
    return RedirectResponse(f"/senders/{sender.id}", status_code=303)


@router.post("/{sender_id}/status")
def set_status(request: Request, sender_id: int, status: str = Form(...), db: Session = Depends(get_db),
               admin: User = Depends(admin_only)):
    sender = get_sender(db, sender_id)
    if status not in ("active", "paused", "disabled"):
        raise HTTPException(status_code=400, detail="Unknown status.")
    audit(db, request, admin, "sender_status_changed", "sender", sender.id, old=sender.status, new=status)
    if status == "active" and sender.status != "active":
        sender.health_since = datetime.now(timezone.utc)  # auto-pause judges only sends from now on
    sender.status = status
    sender.pause_reason = "Paused by admin" if status == "paused" else ""
    db.commit()
    flash(request, f"{sender.from_email} is now {status}.")
    return RedirectResponse(f"/senders/{sender.id}", status_code=303)
