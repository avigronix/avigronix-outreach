"""Pages without login. No CSRF here: mail providers POST the one-click unsubscribe directly."""

import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import get_app_settings
from app.services.audit import audit
from app.services.crypto import read_unsubscribe_token
from app.services.events import handle_sns
from app.services.suppression import suppress
from app.ui import render

router = APIRouter()
log = logging.getLogger("webhooks")


async def raw_body(request: Request) -> bytes:
    return await request.body()


def masked(email: str) -> str:
    local, domain = email.split("@", 1)
    return f"{local[:2]}{'•' * max(1, len(local) - 2)}@{domain}"


@router.get("/u/{token}")
def unsubscribe_page(request: Request, token: str, db: Session = Depends(get_db)):
    email = read_unsubscribe_token(token)
    company = get_app_settings(db).company_name
    if email is None:
        return render(request, "unsubscribe.html", 400, invalid=True, company=company)
    return render(request, "unsubscribe.html", email=masked(email), token=token, company=company)


@router.post("/u/{token}")
def unsubscribe(request: Request, token: str, db: Session = Depends(get_db)):
    """Both the page's button and RFC 8058 one-click (body: List-Unsubscribe=One-Click) land here."""
    email = read_unsubscribe_token(token)
    company = get_app_settings(db).company_name
    if email is None:
        return render(request, "unsubscribe.html", 400, invalid=True, company=company)
    from_page = request.query_params.get("from") == "page"  # the page's form adds ?from=page
    if suppress(db, email, "email", "unsubscribe", "unsubscribe page" if from_page else "one-click"):
        audit(db, request, None, "unsubscribed", "suppression", None, email=email, one_click=not from_page)
    db.commit()
    if not from_page:
        return PlainTextResponse("Unsubscribed")
    return render(request, "unsubscribe.html", done=True, email=masked(email), company=company)


@router.post("/webhooks/ses")
def ses_webhook(body: bytes = Depends(raw_body), db: Session = Depends(get_db)):
    """SES events via SNS. Only messages signed by AWS for our SNS_TOPIC_ARN are accepted."""
    try:
        payload = json.loads(body)
    except ValueError:
        return PlainTextResponse("Bad JSON", status_code=400)
    try:
        result = handle_sns(db, payload)
    except PermissionError as exc:
        log.warning("Rejected SNS message: %s", exc)
        return PlainTextResponse("Forbidden", status_code=403)
    return PlainTextResponse(result)
