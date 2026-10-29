from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role, verify_csrf
from app.config import settings as config
from app.db import get_db
from app.models import AuditLog, User, get_app_settings
from app.services.audit import audit
from app.services.backup import local_backups
from app.ui import flash, render

router = APIRouter(prefix="/settings", dependencies=[Depends(verify_csrf)])
admin_only = require_role("admin")


@router.get("")
def settings_page(request: Request, db: Session = Depends(get_db), admin: User = Depends(admin_only)):
    logs = db.execute(
        select(AuditLog, User.email)
        .outerjoin(User, User.id == AuditLog.user_id)
        .order_by(AuditLog.id.desc())
        .limit(100)
    ).all()
    return render(request, "settings.html", s=get_app_settings(db), logs=logs, backups=local_backups(),
                  bucket=config.backup_s3_bucket)


@router.post("")
def save_settings(
    request: Request,
    company_name: str = Form(...),
    postal_address: str = Form(""),
    cap_day_tz: str = Form(...),
    bounce_pause_pct: float = Form(...),
    complaint_pause_pct: float = Form(...),
    pause_min_sends: int = Form(...),
    domain_daily_max: int = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(admin_only),
):
    try:
        ZoneInfo(cap_day_tz.strip())
    except (ZoneInfoNotFoundError, ValueError):
        flash(request, f"Unknown timezone: {cap_day_tz}. Use a name like America/New_York.", "error")
        return RedirectResponse("/settings", status_code=303)
    if not (0 < bounce_pause_pct <= 100 and 0 < complaint_pause_pct <= 100):
        flash(request, "Pause thresholds must be between 0 and 100%.", "error")
        return RedirectResponse("/settings", status_code=303)
    if pause_min_sends < 1 or domain_daily_max < 1:
        flash(request, "Minimum sends and per-domain limit must be at least 1.", "error")
        return RedirectResponse("/settings", status_code=303)

    s = get_app_settings(db)
    s.company_name = company_name.strip()
    s.postal_address = postal_address.strip()
    s.cap_day_tz = cap_day_tz.strip()
    s.bounce_pause_pct = bounce_pause_pct
    s.complaint_pause_pct = complaint_pause_pct
    s.pause_min_sends = pause_min_sends
    s.domain_daily_max = domain_daily_max
    audit(db, request, admin, "settings_changed", "settings", 1)
    db.commit()
    flash(request, "Settings saved.")
    return RedirectResponse("/settings", status_code=303)
