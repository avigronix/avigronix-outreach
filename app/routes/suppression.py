import csv
import io

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_role, verify_csrf
from app.db import get_db
from app.models import Suppression, User
from app.services.audit import audit
from app.services.suppression import parse_value, suppress
from app.ui import flash, render

router = APIRouter(prefix="/suppression", dependencies=[Depends(verify_csrf)])
managers = require_role("admin", "manager")
admin_only = require_role("admin")

PAGE_SIZE = 50
MAX_IMPORT_BYTES = 5 * 1024 * 1024
REASONS = ("unsubscribe", "hard_bounce", "complaint", "manual", "erasure")


def back():
    return RedirectResponse("/suppression", status_code=303)


@router.get("")
def list_suppressions(
    request: Request, q: str = "", page: int = 1, db: Session = Depends(get_db), user: User = Depends(managers)
):
    stmt = select(Suppression)
    if q.strip():
        stmt = stmt.where(Suppression.value.like(f"%{q.strip().lower()}%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    page = max(1, min(page, (total + PAGE_SIZE - 1) // PAGE_SIZE or 1))
    rows = db.scalars(stmt.order_by(Suppression.id.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    by_reason = dict(db.execute(select(Suppression.reason, func.count()).group_by(Suppression.reason)).all())
    return render(
        request, "suppression.html", rows=rows, total=total, page=page, page_size=PAGE_SIZE, q=q,
        by_reason=by_reason, reasons=REASONS,
    )


@router.post("")
def add_suppression(
    request: Request,
    values: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(managers),
):
    """One or more emails/domains, separated by new lines or commas."""
    added, bad = 0, []
    for raw in values.replace(",", "\n").splitlines():
        if not raw.strip():
            continue
        parsed = parse_value(raw)
        if parsed is None:
            bad.append(raw.strip())
            continue
        added += suppress(db, parsed[0], parsed[1], "manual", "added by hand", user.id)
    audit(db, request, user, "suppression_added", "suppression", None, count=added)
    db.commit()
    flash(request, f"Added {added} entries." + (f" Skipped (not an email or domain): {', '.join(bad[:5])}" if bad else ""),
          "error" if bad and not added else "ok")
    return back()


@router.post("/import")
def import_suppressions(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(managers),
):
    """CSV: every cell in the first column that looks like an email or domain is added."""
    raw = file.file.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        flash(request, "File is larger than 5 MB.", "error")
        return back()
    text = raw.decode("utf-8-sig", errors="replace")
    added = skipped = 0
    for row in csv.reader(io.StringIO(text)):
        parsed = parse_value(row[0]) if row else None
        if parsed is None:
            skipped += 1  # header row or junk
            continue
        added += suppress(db, parsed[0], parsed[1], "manual", f"csv: {file.filename}", user.id)
    audit(db, request, user, "suppression_imported", "suppression", None, file=file.filename, added=added)
    db.commit()
    flash(request, f"Imported {added} new entries ({skipped} rows skipped, duplicates ignored).")
    return back()


@router.get("/export.csv")
def export_suppressions(request: Request, db: Session = Depends(get_db), user: User = Depends(managers)):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["value", "kind", "reason", "source", "added_at"])
    for s in db.scalars(select(Suppression).order_by(Suppression.id)).yield_per(1000):
        writer.writerow([s.value, s.kind, s.reason, s.source, s.created_at.isoformat()])
    audit(db, request, user, "suppression_exported")
    db.commit()
    return Response(
        "﻿" + out.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="suppression-list.csv"'},
    )


@router.post("/{suppression_id}/delete")
def delete_suppression(
    request: Request, suppression_id: int, db: Session = Depends(get_db), user: User = Depends(admin_only)
):
    s = db.get(Suppression, suppression_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Entry not found.")
    audit(db, request, user, "suppression_removed", "suppression", s.id, value=s.value, reason=s.reason)
    db.delete(s)
    db.commit()
    flash(request, f"{s.value} removed from the suppression list. The lead's status was not changed.")
    return back()
