"""Lead import: read .xlsx/.csv, map columns, validate rows, insert leads.

Web side: save_upload() + read_head() at upload time.
Worker side: run_import_jobs() validates (status 'validating') and inserts (status 'importing').
"""

import csv
import io
import json
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import Lead, LeadImport, Suppression
from app.services.validation import (
    check_mx_domains,
    email_domain,
    is_disposable,
    is_free_mail,
    is_role_address,
    normalize_country,
    normalize_email,
    normalize_us_state,
    resolve_timezone,
)

log = logging.getLogger("importer")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_ROWS = 50_000
PREVIEW_ROWS = 20
INSERT_BATCH = 1000

# Lead fields a file column can map to. "custom" stores the value in leads.custom under the column's name.
FIELDS = {
    "email": "Email (required)",
    "first_name": "First name",
    "last_name": "Last name",
    "company": "Company",
    "title": "Title",
    "website": "Website",
    "city": "City",
    "state": "State → sets timezone",
    "country": "Country",
    "timezone": "Timezone",
    "industry": "Industry",
    "notes": "Notes",
    "custom": "Custom field",
}

ALIASES = {
    "email": {"email", "e-mail", "email address", "e-mail address", "work email", "business email", "mail"},
    "first_name": {"first", "first name", "firstname", "given name", "fname"},
    "last_name": {"last", "last name", "lastname", "surname", "family name", "lname"},
    "company": {"company", "company name", "organization", "organisation", "business", "business name",
                "account", "account name", "practice", "practice name", "firm"},
    "title": {"title", "job title", "role", "position", "designation"},
    "website": {"website", "web", "url", "site", "web site", "company website", "domain"},
    "city": {"city", "town"},
    "state": {"state", "province", "region", "state/province"},
    "country": {"country", "country name"},
    "timezone": {"timezone", "time zone", "tz"},
    "industry": {"industry", "sector", "vertical"},
    "notes": {"notes", "note", "comments", "comment"},
}

# Max length per text column, so one over-long cell can't fail a whole insert batch.
TEXT_LIMITS = {c.name: c.type.length for c in Lead.__table__.columns if getattr(c.type, "length", None)}

REJECT_REASONS = {
    "missing_email": "Missing email",
    "invalid_email": "Invalid email",
    "duplicate_in_file": "Duplicate in file",
    "duplicate_in_db": "Already in database",
    "suppressed": "On suppression list",
    "disposable": "Disposable domain",
    "no_mx": "Domain has no MX record",
}


# ---------- reading files ----------

def cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value).strip()


def iter_rows(path: str):
    """Yield every row of the first sheet / the CSV as a list of strings (header row included)."""
    if path.lower().endswith(".xlsx"):
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            for row in wb.worksheets[0].iter_rows(values_only=True):
                yield [cell_text(v) for v in row]
        finally:
            wb.close()
        return

    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:5000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    for row in csv.reader(io.StringIO(text), dialect):
        yield [cell.strip() for cell in row]


def clean_headers(raw: list[str]) -> list[str]:
    headers, seen = [], {}
    for i, h in enumerate(raw):
        h = h or f"Column {i + 1}"
        seen[h] = seen.get(h, 0) + 1
        headers.append(h if seen[h] == 1 else f"{h} ({seen[h]})")
    return headers


def read_head(path: str) -> tuple[list[str], list[list[str]], int]:
    """(headers, first 20 data rows, number of data rows). Blank rows are skipped."""
    headers, preview, total = None, [], 0
    for row in iter_rows(path):
        if not any(row):
            continue
        if headers is None:
            headers = clean_headers(row)
            continue
        total += 1
        if len(preview) < PREVIEW_ROWS:
            preview.append(row)
        if total > MAX_ROWS:
            break
    return headers or [], preview, total


def save_upload(fileobj, import_id: int, file_name: str) -> str:
    """Copy the upload to UPLOAD_DIR in chunks; raises ValueError over 10 MB."""
    folder = Path(settings.upload_dir) / "imports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{import_id}{Path(file_name).suffix.lower()}"
    size = 0
    with open(path, "wb") as out:
        while chunk := fileobj.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                out.close()
                path.unlink(missing_ok=True)
                raise ValueError("File is larger than 10 MB.")
            out.write(chunk)
    return str(path)


def parse_tags(raw: str) -> list[str]:
    """'US Dental, oct-2026' → ['us-dental', 'oct-2026']"""
    tags = [" ".join(t.split()).lower().replace(" ", "-") for t in raw.split(",")]
    return sorted({t for t in tags if t})


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "field"


def auto_map(headers: list[str]) -> dict[str, str]:
    """Guess the field for each column by its header. Returns {column index (str): field}."""
    mapping, used = {}, set()
    for i, header in enumerate(headers):
        key = re.sub(r"[_\-]+", " ", header.lower()).strip()
        for field, names in ALIASES.items():
            if field not in used and key in names:
                mapping[str(i)] = field
                used.add(field)
                break
    return mapping


def mapping_problem(column_map: dict[str, str]) -> str | None:
    fields = [f for f in column_map.values() if f and f != "custom"]
    if "email" not in fields:
        return "Map one column to Email."
    duplicates = sorted({f for f in fields if fields.count(f) > 1})
    if duplicates:
        return "Each field can be used once: " + ", ".join(FIELDS[f] for f in duplicates)
    return None


# ---------- validation ----------

def row_to_lead(row: list[str], headers: list[str], column_map: dict[str, str]) -> dict:
    """Apply the column map to one row → dict of lead fields (+ custom)."""
    lead = {f: "" for f in FIELDS if f != "custom"}
    lead["custom"] = {}
    for index, field in column_map.items():
        i = int(index)
        value = row[i] if i < len(row) else ""
        if field == "custom":
            if value:
                lead["custom"][slug(headers[i])] = value
        elif field:
            lead[field] = value
    return lead


def validate_rows(
    rows: list[list[str]],
    headers: list[str],
    column_map: dict[str, str],
    existing_emails: set[str],
    suppressed: set[str],
    mx_for_domains,
) -> tuple[list[dict], list[tuple[list[str], str]], dict]:
    """Pure validation of all rows.

    existing_emails / suppressed: emails (and domains, for suppressed) already in the database.
    mx_for_domains(set_of_domains) -> {domain: True/False/None}.
    Returns (ready leads, rejected (row, reason) pairs, counts).
    """
    counts = {"total": len(rows), "ready": 0, "duplicates": 0, "suppressed": 0, "invalid": 0,
              "role": 0, "free_mail": 0, "tz_found": 0}
    candidates, rejected, seen = [], [], set()

    for row in rows:
        lead = row_to_lead(row, headers, column_map)
        if not lead["email"]:
            rejected.append((row, "missing_email"))
            continue
        email = normalize_email(lead["email"])
        if email is None:
            rejected.append((row, "invalid_email"))
            continue
        domain = email_domain(email)
        if email in seen:
            rejected.append((row, "duplicate_in_file"))
            continue
        seen.add(email)
        if email in existing_emails:
            rejected.append((row, "duplicate_in_db"))
            continue
        if email in suppressed or domain in suppressed:
            rejected.append((row, "suppressed"))
            continue
        if is_disposable(domain):
            rejected.append((row, "disposable"))
            continue
        lead["email"], lead["domain"] = email, domain
        candidates.append((row, lead))

    mx = mx_for_domains({lead["domain"] for _, lead in candidates})
    ready = []
    for row, lead in candidates:
        if mx.get(lead["domain"]) is False:
            rejected.append((row, "no_mx"))
            continue
        for field, limit in TEXT_LIMITS.items():
            if isinstance(lead.get(field), str):
                lead[field] = lead[field][:limit]
        lead["country"] = normalize_country(lead["country"])
        if lead["country"] in ("US", ""):
            lead["state"] = normalize_us_state(lead["state"])
        lead["timezone"], found = resolve_timezone(lead["timezone"], lead["state"], lead["country"])
        lead["is_role"] = is_role_address(lead["email"])
        lead["is_free_mail"] = is_free_mail(lead["domain"])
        counts["tz_found"] += found
        counts["role"] += lead["is_role"]
        counts["free_mail"] += lead["is_free_mail"]
        ready.append(lead)

    for _, reason in rejected:
        if reason in ("duplicate_in_file", "duplicate_in_db"):
            counts["duplicates"] += 1
        elif reason == "suppressed":
            counts["suppressed"] += 1
        else:
            counts["invalid"] += 1
    counts["ready"] = len(ready)
    return ready, rejected, counts


# ---------- worker jobs ----------

def ready_path(imp: LeadImport) -> Path:
    return Path(imp.stored_path).with_suffix(".ready.jsonl")


def rejected_path(imp: LeadImport) -> Path:
    return Path(imp.stored_path).with_suffix(".rejected.csv")


def _existing(db: Session, model_column, values: list[str]) -> set[str]:
    found = set()
    for i in range(0, len(values), 1000):
        found.update(db.scalars(select(model_column).where(model_column.in_(values[i : i + 1000]))))
    return found


def validate_import(db: Session, imp: LeadImport) -> None:
    rows = [row for row in iter_rows(imp.stored_path) if any(row)][1:]  # drop header
    if len(rows) > MAX_ROWS:
        raise ValueError(f"File has more than {MAX_ROWS:,} rows.")

    email_index = next(int(i) for i, f in imp.column_map.items() if f == "email")
    emails = sorted({e for row in rows if email_index < len(row) and (e := normalize_email(row[email_index]))})
    domains = sorted({email_domain(e) for e in emails})
    existing = _existing(db, Lead.email, emails)
    suppressed = _existing(db, Suppression.value, emails + domains)

    ready, rejected, counts = validate_rows(
        rows, imp.headers, imp.column_map, existing, suppressed, lambda ds: check_mx_domains(db, ds)
    )

    with open(ready_path(imp), "w", encoding="utf-8") as out:
        for lead in ready:
            out.write(json.dumps(lead) + "\n")
    with open(rejected_path(imp), "w", encoding="utf-8-sig", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(imp.headers + ["Reject reason"])
        for row, reason in rejected:
            writer.writerow(row + [""] * (len(imp.headers) - len(row)) + [REJECT_REASONS[reason]])

    imp.counts = counts
    imp.total = len(rows)
    imp.processed = len(rows)
    imp.status = "validated"


def insert_import(db: Session, imp: LeadImport) -> None:
    """Insert the validated leads. Safe to re-run after a crash: existing emails are skipped."""
    with open(ready_path(imp), encoding="utf-8") as f:
        leads = [json.loads(line) for line in f]

    for i in range(0, len(leads), INSERT_BATCH):
        batch = [
            {**lead, "tags": imp.tags, "source_type": imp.source_type, "import_id": imp.id, "status": "new",
             "soft_bounce_count": 0}
            for lead in leads[i : i + INSERT_BATCH]
        ]
        db.execute(insert(Lead).values(batch).on_conflict_do_nothing(index_elements=["email"]))
        imp.processed = min(i + INSERT_BATCH, len(leads))
        db.commit()

    imp.imported = db.scalar(select(func.count()).select_from(Lead).where(Lead.import_id == imp.id))
    imp.status = "done"
    imp.finished_at = datetime.now(timezone.utc)


def run_import_jobs() -> None:
    """Worker job: handle every import waiting for validation or insert.

    SKIP LOCKED only guards the claim; validation and insert commit as they go and are
    safe to re-run, so a crash simply resumes on the next loop."""
    while True:
        with SessionLocal() as db:
            imp = db.scalar(
                select(LeadImport)
                .where(LeadImport.status.in_(("validating", "importing")))
                .order_by(LeadImport.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if imp is None:
                return
            log.info("Import %s: %s", imp.id, imp.status)
            try:
                if imp.status == "validating":
                    validate_import(db, imp)
                else:
                    insert_import(db, imp)
            except Exception as exc:
                log.exception("Import %s failed", imp.id)
                db.rollback()
                imp = db.get(LeadImport, imp.id)
                imp.status, imp.error = "failed", str(exc)[:500]
            db.commit()
