from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import (
    client_ip,
    hash_password,
    login_session,
    needs_rehash,
    session_is_valid,
    too_many_failed_logins,
    verify_csrf,
    verify_password,
)
from app.db import get_db
from app.models import User
from app.services.audit import audit
from app.ui import render

router = APIRouter(dependencies=[Depends(verify_csrf)])


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if user_id and session_is_valid(request.session, db.get(User, user_id)):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    ip = client_ip(request)

    if too_many_failed_logins(db, ip, user):
        return render(request, "login.html", 429, email=email, error="Too many attempts. Wait a minute and try again.")

    if user is None or not user.is_active or not verify_password(user.password_hash, password):
        audit(db, request, None, "login_failed", "user", user.id if user else None, email=email)
        db.commit()
        return render(request, "login.html", 400, email=email, error="Wrong email or password.")

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = datetime.now(timezone.utc)
    audit(db, request, user, "login", "user", user.id)
    db.commit()
    login_session(request, user)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
