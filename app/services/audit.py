from fastapi import Request
from sqlalchemy.orm import Session

from app.auth import client_ip
from app.models import AuditLog, User


def audit(
    db: Session,
    request: Request | None,
    user: User | None,
    action: str,
    entity: str | None = None,
    entity_id: int | None = None,
    **meta,
) -> None:
    """Add an audit row to the session; the caller commits it together with the change."""
    db.add(
        AuditLog(
            user_id=user.id if user else None,
            action=action,
            entity=entity,
            entity_id=entity_id,
            ip=client_ip(request) if request else None,
            meta=meta,
        )
    )
