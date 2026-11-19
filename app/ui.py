"""Jinja2 setup and the `render` helper every page uses."""

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.auth import csrf_token
from app.config import settings
from app.db import SessionLocal
from app.services.replies import needs_action_count
from app.services.scheduler import is_business_day

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# (key, label, url, roles that see it, built yet?)
NAV = [
    ("dashboard", "Dashboard", "/", ("admin", "manager", "sales"), True),
    ("replies", "Replies", "/replies", ("admin", "manager", "sales"), True),
    ("leads", "Leads", "/leads", ("admin", "manager", "sales"), True),
    ("campaigns", "Campaigns", "/campaigns", ("admin", "manager"), True),
    ("templates", "Templates", "/templates", ("admin", "manager"), True),
    ("imports", "Imports", "/imports", ("admin", "manager"), True),
    ("reports", "Reports", "/reports", ("admin", "manager"), True),
    ("senders", "Senders", "/senders", ("admin",), True),
    ("suppression", "Suppression list", "/suppression", ("admin", "manager"), True),
    ("users", "Users", "/users", ("admin",), True),
    ("settings", "Settings", "/settings", ("admin",), True),
]
# Sidebar group of each item; a heading is shown where the group changes.
NAV_GROUPS = {"dashboard": "Workspace", "replies": "Workspace", "leads": "Workspace",
              "campaigns": "Outreach", "templates": "Outreach", "imports": "Outreach", "reports": "Outreach",
              "senders": "Admin", "suppression": "Admin", "users": "Admin", "settings": "Admin"}


def local_time(value: datetime | None, fmt: str = "%d %b %Y, %H:%M") -> str:
    """Show a UTC timestamp in IST (DISPLAY_TZ)."""
    if value is None:
        return "—"
    return value.astimezone(ZoneInfo(settings.display_tz)).strftime(fmt)


templates.env.filters["local"] = local_time


def today_ist() -> date:
    """The team's own calendar day (DISPLAY_TZ), used for follow-up reminders."""
    return datetime.now(ZoneInfo(settings.display_tz)).date()

OFFICE_HOURS = (9, 17)  # lead's local time, for the "good time to reply?" dot


def lead_clock(tz_name: str | None, now: datetime | None = None) -> dict | None:
    """The lead's time right now and whether it is inside office hours there (Mon–Fri, 9–17, no US holiday)."""
    if not tz_name:
        return None
    try:
        here = (now or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        return None
    open_now = is_business_day(here.date()) and OFFICE_HOURS[0] <= here.hour < OFFICE_HOURS[1]
    return {"time": here.strftime("%I:%M %p").lstrip("0"), "zone": here.strftime("%Z"), "day": here.strftime("%a"),
            "open": open_now}


templates.env.filters["lead_clock"] = lead_clock


def in_zone(value: datetime | None, tz_name: str | None, fmt: str = "%a %I:%M %p") -> str:
    """A UTC timestamp in the lead's own time zone ("" when either is unknown)."""
    if value is None or not tz_name:
        return ""
    try:
        return value.astimezone(ZoneInfo(tz_name)).strftime(fmt).replace(" 0", " ")
    except (ZoneInfoNotFoundError, ValueError):
        return ""


templates.env.filters["in_zone"] = in_zone


def initials(name: str | None) -> str:
    parts = (name or "?").split()
    return "".join(p[0] for p in parts[:2]).upper() or "?"


templates.env.filters["initials"] = initials


def flash(request: Request, message: str, kind: str = "ok", undo: str | None = None) -> None:
    """Message shown once on the next rendered page, as a toast. kind: ok / error.
    `undo` = a URL the toast's Undo button POSTs to."""
    request.session["flashes"] = request.session.get("flashes", []) + [[kind, message, undo]]


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
        nav_groups=NAV_GROUPS,
        badges=nav_badges(user),
    )
    return templates.TemplateResponse(request, name, context, status_code=status_code)
