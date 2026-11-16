from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.auth import LoginRequired
from app.config import settings
from app.routes import (
    auth, campaigns, dashboard, imports, leads, public, replies, reports, senders, suppression, templates, users,
)
from app.routes import settings as settings_routes
from app.ui import render

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="outreach_session",
    max_age=7 * 24 * 3600,
    same_site="lax",
    https_only=settings.cookie_secure,
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(users.router)
app.include_router(settings_routes.router)
app.include_router(leads.router)
app.include_router(imports.router)
app.include_router(suppression.router)
app.include_router(templates.router)
app.include_router(senders.router)
app.include_router(campaigns.router)
app.include_router(replies.router)
app.include_router(reports.router)
app.include_router(public.router)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.exception_handler(LoginRequired)
def login_required(request: Request, exc: LoginRequired):
    if request.headers.get("hx-request"):
        return Response(status_code=204, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(StarletteHTTPException)
def http_error(request: Request, exc: StarletteHTTPException):
    return render(request, "error.html", exc.status_code, message=exc.detail)
