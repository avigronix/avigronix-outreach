from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role, verify_csrf
from app.db import get_db
from app.models import SOURCE_TYPES, LeadImport, User
from app.services import importer
from app.services.audit import audit
from app.ui import flash, form_data, render

router = APIRouter(prefix="/imports", dependencies=[Depends(verify_csrf)])
importers = require_role("admin", "manager")


def get_import(db: Session, import_id: int) -> LeadImport:
    imp = db.get(LeadImport, import_id)
    if imp is None:
        raise HTTPException(status_code=404, detail="Import not found.")
    return imp


@router.get("")
def list_imports(request: Request, db: Session = Depends(get_db), user: User = Depends(importers)):
    imports = db.scalars(select(LeadImport).order_by(LeadImport.id.desc()).limit(100)).all()
    return render(request, "imports/list.html", imports=imports, source_types=SOURCE_TYPES)


@router.get("/new")
def upload_page(request: Request, user: User = Depends(importers)):
    return render(request, "imports/upload.html")


@router.post("")
def upload(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(importers),
):
    name = Path(file.filename or "").name
    if not name.lower().endswith((".xlsx", ".csv")):
        flash(request, "Upload an .xlsx or .csv file (old .xls: save it as .xlsx first).", "error")
        return RedirectResponse("/imports/new", status_code=303)

    imp = LeadImport(file_name=name, stored_path="", uploaded_by=user.id)
    db.add(imp)
    db.flush()
    try:
        imp.stored_path = importer.save_upload(file.file, imp.id, name)
        headers, preview, total = importer.read_head(imp.stored_path)
        if total == 0:
            raise ValueError("The file has no data rows.")
        if total > importer.MAX_ROWS:
            raise ValueError(f"The file has more than {importer.MAX_ROWS:,} rows. Split it into smaller files.")
    except Exception as exc:
        db.rollback()
        if imp.stored_path:
            Path(imp.stored_path).unlink(missing_ok=True)
        message = str(exc) if isinstance(exc, ValueError) else "Could not read this file. Is it a valid .xlsx/.csv?"
        flash(request, message, "error")
        return RedirectResponse("/imports/new", status_code=303)

    imp.headers, imp.preview, imp.total = headers, preview, total
    imp.column_map = importer.auto_map(headers)
    audit(db, request, user, "import_uploaded", "lead_import", imp.id, file=name, rows=total)
    db.commit()
    return RedirectResponse(f"/imports/{imp.id}/map", status_code=303)


@router.get("/{import_id}/map")
def map_page(request: Request, import_id: int, db: Session = Depends(get_db), user: User = Depends(importers)):
    imp = get_import(db, import_id)
    if imp.status not in ("uploaded", "validated", "failed"):
        return RedirectResponse(f"/imports/{imp.id}", status_code=303)
    samples = []
    for i in range(len(imp.headers)):
        samples.append(next((row[i] for row in imp.preview if i < len(row) and row[i]), ""))
    matched = sum(1 for f in imp.column_map.values() if f)
    return render(
        request, "imports/map.html", imp=imp, samples=samples, fields=importer.FIELDS,
        source_types=SOURCE_TYPES, matched=matched,
    )


@router.post("/{import_id}/map")
def save_map(
    request: Request,
    import_id: int,
    form=Depends(form_data),
    db: Session = Depends(get_db),
    user: User = Depends(importers),
):
    imp = get_import(db, import_id)
    if imp.status not in ("uploaded", "validated", "failed"):
        return RedirectResponse(f"/imports/{imp.id}", status_code=303)
    column_map = {
        str(i): form.get(f"map_{i}", "")
        for i in range(len(imp.headers))
        if form.get(f"map_{i}", "") in importer.FIELDS
    }
    source_type = form.get("source_type", "")
    problem = importer.mapping_problem(column_map)
    if source_type not in SOURCE_TYPES:
        problem = "Choose where these leads came from (source type)."
    imp.column_map = column_map
    imp.list_name = str(form.get("list_name", "")).strip()[:200]
    imp.tags = importer.parse_tags(str(form.get("tags", "")))
    imp.source_type = source_type or None
    if problem:
        db.commit()
        flash(request, problem, "error")
        return RedirectResponse(f"/imports/{imp.id}/map", status_code=303)

    imp.status, imp.error, imp.counts, imp.processed = "validating", None, {}, 0
    db.commit()
    return RedirectResponse(f"/imports/{imp.id}", status_code=303)


@router.get("/{import_id}")
def status_page(request: Request, import_id: int, db: Session = Depends(get_db), user: User = Depends(importers)):
    imp = get_import(db, import_id)
    if imp.status == "uploaded":
        return RedirectResponse(f"/imports/{imp.id}/map", status_code=303)
    return render(request, "imports/status.html", imp=imp, source_types=SOURCE_TYPES)


@router.get("/{import_id}/progress")
def progress(request: Request, import_id: int, db: Session = Depends(get_db), user: User = Depends(importers)):
    """HTMX fragment, polled every 2 s while the worker is busy; reloads the page once it is finished."""
    imp = get_import(db, import_id)
    if imp.status not in ("validating", "importing"):
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return render(request, "imports/_progress.html", imp=imp)


@router.post("/{import_id}/confirm")
def confirm(request: Request, import_id: int, db: Session = Depends(get_db), user: User = Depends(importers)):
    imp = get_import(db, import_id)
    if imp.status != "validated":
        flash(request, "This import is not waiting for confirmation.", "error")
        return RedirectResponse(f"/imports/{imp.id}", status_code=303)
    imp.status, imp.processed = "importing", 0
    audit(db, request, user, "import_confirmed", "lead_import", imp.id, ready=imp.counts.get("ready"))
    db.commit()
    return RedirectResponse(f"/imports/{imp.id}", status_code=303)


@router.get("/{import_id}/rejected.csv")
def rejected_csv(import_id: int, db: Session = Depends(get_db), user: User = Depends(importers)):
    imp = get_import(db, import_id)
    path = importer.rejected_path(imp)
    if not imp.stored_path or not path.exists():
        raise HTTPException(status_code=404, detail="No rejected rows file yet.")
    return FileResponse(path, media_type="text/csv", filename=f"rejected-{Path(imp.file_name).stem}.csv")
