"""My account: every user (any role) can change their own name and password."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import current_user, hash_password, login_session, password_problem, verify_csrf, verify_password
from app.db import get_db
from app.models import User
from app.services.audit import audit
from app.ui import flash, render

router = APIRouter(prefix="/account", dependencies=[Depends(verify_csrf)])


@router.get("")
def account_page(request: Request, user: User = Depends(current_user)):
    return render(request, "account.html")


@router.post("/name")
def change_name(request: Request, name: str = Form(""), db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    name = name.strip()
    if not name:
        return render(request, "account.html", 400, name_error="Type your name.")
    user.name = name[:120]
    db.commit()
    flash(request, "Name saved.")
    return RedirectResponse("/account", status_code=303)


@router.post("/password")
def change_password(
    request: Request,
    current: str = Form(""),
    new: str = Form(""),
    confirm: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    if not verify_password(user.password_hash, current):
        error = "Your current password is not right."
    elif new != confirm:
        error = "The two new passwords are not the same."
    else:
        error = password_problem(new)
    if error:
        return render(request, "account.html", 400, password_error=error)
    user.password_hash = hash_password(new)
    user.session_version += 1  # every other device is logged out
    audit(db, request, user, "password_changed", "user", user.id)
    db.commit()
    login_session(request, user)  # this browser stays logged in
    flash(request, "Password changed. Other devices were logged out.")
    return RedirectResponse("/account", status_code=303)
