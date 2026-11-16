"""The one place that decides "may we email this address?" (core rule 1)."""

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Enrollment, Lead, Suppression
from app.services.validation import email_domain, normalize_email

# Lead status set when the lead's own email is suppressed for this reason.
STATUS_FOR_REASON = {
    "unsubscribe": "unsubscribed",
    "hard_bounce": "bounced",
    "complaint": "complained",
}


def stop_lead(db: Session, lead_id: int, reason: str) -> None:
    """End the lead's active enrollment (stop rule 5). The caller commits."""
    db.execute(
        update(Enrollment).where(Enrollment.lead_id == lead_id, Enrollment.state == "active")
        .values(state="stopped", stop_reason=reason[:40], next_send_at=None)
    )


def suppression_keys(email: str) -> list[str]:
    """An email is blocked if the address itself or its domain is suppressed."""
    email = email.strip().lower()
    return [email, email_domain(email)]


def is_suppressed(db: Session, email: str) -> bool:
    return db.scalar(select(Suppression.id).where(Suppression.value.in_(suppression_keys(email))).limit(1)) is not None


def parse_value(raw: str) -> tuple[str, str] | None:
    """'Jane@X.com' → ('jane@x.com', 'email'); '@x.com' or 'x.com' → ('x.com', 'domain'); junk → None."""
    raw = raw.strip().lower()
    if "@" in raw.lstrip("@"):
        email = normalize_email(raw)
        return (email, "email") if email else None
    domain = raw.lstrip("@").strip(".")
    if "." in domain and " " not in domain and len(domain) <= 255:
        return domain, "domain"
    return None


def suppress(db: Session, value: str, kind: str, reason: str, source: str = "", user_id: int | None = None) -> bool:
    """Add to the suppression list (no-op if already there). Returns True if newly added.
    The lead with this email gets a matching status. The caller commits."""
    new_id = db.scalar(
        insert(Suppression)
        .values(value=value, kind=kind, reason=reason, source=source[:100], created_by=user_id)
        .on_conflict_do_nothing(index_elements=["value"])
        .returning(Suppression.id)
    )
    if kind == "email":
        lead = db.scalar(select(Lead).where(Lead.email == value))
        if lead is not None:
            lead.status = STATUS_FOR_REASON.get(reason, "do_not_contact")
            stop_lead(db, lead.id, STATUS_FOR_REASON.get(reason, "suppressed"))
    # Domain entries: enrollments stop at their next send-time check.
    return new_id is not None
