"""The Ctrl+K search box: pages, leads, campaigns and templates the user may open."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import current_user
from app.db import get_db
from app.models import Campaign, Lead, Template, User
from app.ui import NAV, render

router = APIRouter()
PER_GROUP = 5


def like_pattern(q: str) -> str:
    escaped = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/search")
def search(request: Request, q: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    """HTMX partial. Empty query → the pages this user can open; otherwise matches in every group."""
    q = q.strip()
    pages = [(label, url) for key, label, url, roles, ready in NAV if ready and user.role in roles]
    pages.append(("My account", "/account"))
    groups = []  # (group name, [(icon, title, subtitle, url)])
    if q:
        pages = [(label, url) for label, url in pages if q.lower() in label.lower()]
    groups.append(("Pages", [(url.strip("/") or "dashboard", label, "", url) for label, url in pages]))

    if q:
        like = like_pattern(q)
        leads = select(Lead).where(or_(
            Lead.email.like(like, escape="\\"),
            func.lower(Lead.company).like(like, escape="\\"),
            func.lower(Lead.first_name + " " + Lead.last_name).like(like, escape="\\"),
        ))
        if user.role == "sales":
            leads = leads.where(Lead.owner_id == user.id)
        groups.append(("Leads", [
            ("leads", (lead.first_name + " " + lead.last_name).strip() or lead.email,
             " · ".join(x for x in (lead.email, lead.company) if x), f"/leads/{lead.id}")
            for lead in db.scalars(leads.order_by(Lead.id.desc()).limit(PER_GROUP))]))
        if user.role in ("admin", "manager"):
            groups.append(("Campaigns", [
                ("campaigns", c.name, c.status.capitalize(), f"/campaigns/{c.id}")
                for c in db.scalars(select(Campaign).where(func.lower(Campaign.name).like(like, escape="\\"))
                                    .order_by(Campaign.id.desc()).limit(PER_GROUP))]))
            groups.append(("Templates", [
                ("templates", t.name, (t.subject_variants or [""])[0], f"/templates/{t.id}")
                for t in db.scalars(select(Template).where(func.lower(Template.name).like(like, escape="\\"))
                                    .order_by(Template.name).limit(PER_GROUP))]))
    groups = [(name, items) for name, items in groups if items]
    return render(request, "_search.html", groups=groups, q=q)
