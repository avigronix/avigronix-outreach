from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import hash_password, password_problem, require_role, verify_csrf
from app.db import get_db
from app.models import ROLES, User
from app.services.audit import audit
from app.ui import flash, render

router = APIRouter(prefix="/users", dependencies=[Depends(verify_csrf)])
admin_only = require_role("admin")


def back_to_list():
    return RedirectResponse("/users", status_code=303)


def get_user_or_404(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    return user


@router.get("")
def list_users(request: Request, db: Session = Depends(get_db), admin: User = Depends(admin_only)):
    users = db.scalars(select(User).order_by(User.is_active.desc(), User.name)).all()
    return render(request, "users.html", users=users, roles=ROLES)


@router.post("")
def create_user(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    role: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(admin_only),
):
    try:
        email = validate_email(email.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        flash(request, "That email address is not valid.", "error")
        return back_to_list()
    problem = password_problem(password)
    if role not in ROLES:
        problem = "Unknown role."
    elif db.scalar(select(User.id).where(func.lower(User.email) == email)):
        problem = f"A user with {email} already exists."
    if problem:
        flash(request, problem, "error")
        return back_to_list()

    user = User(name=name.strip(), email=email, role=role, password_hash=hash_password(password))
    db.add(user)
    db.flush()
    audit(db, request, admin, "user_created", "user", user.id, email=email, role=role)
    db.commit()
    flash(request, f"User {email} created.")
    return back_to_list()


@router.post("/{user_id}/role")
def change_role(
    request: Request,
    user_id: int,
    role: str = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(admin_only),
):
    user = get_user_or_404(db, user_id)
    if role not in ROLES:
        raise HTTPException(status_code=400, detail="Unknown role.")
    if user.id == admin.id:
        flash(request, "You can't change your own role.", "error")
        return back_to_list()
    audit(db, request, admin, "user_role_changed", "user", user.id, old=user.role, new=role)
    user.role = role
    db.commit()
    flash(request, f"{user.email} is now {role}.")
    return back_to_list()


@router.post("/{user_id}/toggle")
def toggle_active(request: Request, user_id: int, db: Session = Depends(get_db), admin: User = Depends(admin_only)):
    user = get_user_or_404(db, user_id)
    if user.id == admin.id:
        flash(request, "You can't disable yourself.", "error")
        return back_to_list()
    user.is_active = not user.is_active
    user.session_version += 1  # logs the user out everywhere
    audit(db, request, admin, "user_enabled" if user.is_active else "user_disabled", "user", user.id)
    db.commit()
    flash(request, f"{user.email} {'enabled' if user.is_active else 'disabled'}.")
    return back_to_list()


@router.post("/{user_id}/reset")
def reset_password(
    request: Request,
    user_id: int,
    password: str = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(admin_only),
):
    user = get_user_or_404(db, user_id)
    problem = password_problem(password)
    if problem:
        flash(request, problem, "error")
        return back_to_list()
    user.password_hash = hash_password(password)
    user.session_version += 1
    audit(db, request, admin, "user_password_reset", "user", user.id)
    db.commit()
    if user.id == admin.id:
        request.session["sv"] = user.session_version  # keep the admin's own session alive
    flash(request, f"Password reset for {user.email}. Share it with them privately.")
    return back_to_list()
