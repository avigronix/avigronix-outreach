"""Jinja2 setup and the `render` helper every page uses."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.auth import csrf_token
from app.config import settings
from app.db import SessionLocal
from app.services.replies import needs_action_count

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# (key, label, url, roles that see it, built yet?)
NAV = [
    ("dashboard", "Dashboard", "/", ("admin", "manager", "sales"), True),
    ("leads", "Leads", "/leads", ("admin", "manager", "sales"), True),
    ("imports", "Imports", "/imports", ("admin", "manager"), True),
    ("templates", "Templates", "/templates", ("admin", "manager"), True),
    ("campaigns", "Campaigns", "/campaigns", ("admin", "manager"), True),
    ("replies", "Replies", "/replies", ("admin", "manager", "sales"), True),
    ("senders", "Senders", "/senders", ("admin",), True),
    ("suppression", "Suppression list", "/suppression", ("admin", "manager"), True),
    ("reports", "Reports", "/reports", ("admin", "manager"), True),
    ("users", "Users", "/users", ("admin",), True),
    ("settings", "Settings", "/settings", ("admin",), True),
]


def local_time(value: datetime | None, fmt: str = "%d %b %Y, %H:%M") -> str:
    """Show a UTC timestamp in IST (DISPLAY_TZ)."""
    if value is None:
        return "—"
    return value.astimezone(ZoneInfo(settings.display_tz)).strftime(fmt)


templates.env.filters["local"] = local_time


def flash(request: Request, message: str, kind: str = "ok") -> None:
    """Message shown once on the next rendered page. kind: ok / error."""
    request.session["flashes"] = request.session.get("flashes", []) + [[kind, message]]


def nav_badges(user) -> dict[str, int]:
    """Counts shown next to sidebar items (replies waiting for someone)."""
    if user is None:
        return {}
    with SessionLocal() as db:
        return {"replies": needs_action_count(db, user)}


async def form_data(request: Request):
    """Dependency for sync endpoints that need a form with dynamic field names."""
    return await request.form()


def render(request: Request, name: str, status_code: int = 200, **context):
    user = getattr(request.state, "user", None)
    context.update(
        user=user,
        csrf_token=csrf_token(request),
        flashes=request.session.pop("flashes", []),
        nav=[item for item in NAV if user and user.role in item[3]],
        badges=nav_badges(user),
    )
    return templates.TemplateResponse(request, name, context, status_code=status_code)
