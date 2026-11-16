"""Passwords, the logged-in user, role checks and CSRF."""

import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AuditLog, User

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 10
MAX_FAILED_LOGINS_PER_MINUTE = 5


class LoginRequired(Exception):
    """Raised by `current_user`; main.py turns it into a redirect to /login."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


def has_role(user: User | None, *roles: str) -> bool:
    return user is not None and user.is_active and user.role in roles


def session_is_valid(session: dict, user: User | None) -> bool:
    """A session is valid only for an active user whose session_version still matches."""
    return user is not None and user.is_active and session.get("sv") == user.session_version


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if not session_is_valid(request.session, user):
        request.session.pop("user_id", None)
        request.session.pop("sv", None)
        raise LoginRequired()
    request.state.user = user
    return user


def require_role(*roles: str):
    """Dependency factory: `Depends(require_role("admin"))`."""

    def dependency(user: User = Depends(current_user)) -> User:
        if not has_role(user, *roles):
            raise HTTPException(status_code=403, detail="You don't have access to this page.")
        return user

    return dependency


def login_session(request: Request, user: User) -> None:
    """Start a fresh session (new CSRF token) for a user who just logged in."""
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["sv"] = user.session_version
    request.session["csrf"] = secrets.token_urlsafe(32)


def too_many_failed_logins(db: Session, ip: str, user: User | None) -> bool:
    """Failed logins are rows in audit_logs; more than 5 in a minute (per IP or per account) blocks login."""
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    who = [AuditLog.ip == ip]
    if user is not None:
        who.append(AuditLog.entity_id == user.id)
    count = db.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "login_failed", AuditLog.created_at >= since, or_(*who))
    )
    return count >= MAX_FAILED_LOGINS_PER_MINUTE


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def verify_csrf(request: Request) -> None:
    """Router dependency: every non-GET request must carry the session's CSRF token
    (form field `csrf_token`, or header `X-CSRF-Token` which base.html sets for HTMX)."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.headers.get("x-csrf-token")
    if not sent:
        form = await request.form()
        sent = form.get("csrf_token")
    expected = request.session.get("csrf")
    if not sent or not expected or not secrets.compare_digest(str(sent), expected):
        raise HTTPException(status_code=403, detail="Form expired. Please reload the page and try again.")
