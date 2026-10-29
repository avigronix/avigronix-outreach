"""Delivery events (core rules 6 and 8): SNS signature check, SES bounce/complaint handling, auto-pause."""

import base64
import json
import logging
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import Campaign, Event, Lead, Message, Sender, get_app_settings
from app.services.alerts import send_alert
from app.services.audit import audit
from app.services.suppression import suppress

log = logging.getLogger("events")

UTC = timezone.utc
SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")
SOFT_BOUNCES_TO_HARD = 3
HEALTH_WINDOW = 200  # sends
_cert_cache: dict[str, x509.Certificate] = {}


# ---------- SNS signature (core rule 8) ----------

def is_sns_url(url: str) -> bool:
    parsed = urlparse(url or "")
    return parsed.scheme == "https" and bool(SNS_HOST.match(parsed.hostname or ""))


def fetch_cert(url: str) -> x509.Certificate:
    if url not in _cert_cache:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 — host checked by is_sns_url
            _cert_cache[url] = x509.load_pem_x509_certificate(resp.read())
    return _cert_cache[url]


def signing_string(payload: dict) -> bytes:
    if payload.get("Type") == "Notification":
        keys = ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"]
    else:  # SubscriptionConfirmation / UnsubscribeConfirmation
        keys = ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"]
    return "".join(f"{k}\n{payload[k]}\n" for k in keys if k in payload).encode()


def verify_sns(payload: dict) -> bool:
    """True only for a message signed by AWS SNS (certificate from an sns.*.amazonaws.com URL)."""
    cert_url = payload.get("SigningCertURL") or payload.get("SigningCertUrl") or ""
    if not is_sns_url(cert_url) or "Signature" not in payload:
        return False
    algorithm = {"1": hashes.SHA1(), "2": hashes.SHA256()}.get(str(payload.get("SignatureVersion")))
    if algorithm is None:
        return False
    try:
        cert = fetch_cert(cert_url)
        cert.public_key().verify(base64.b64decode(payload["Signature"]), signing_string(payload),
                                 padding.PKCS1v15(), algorithm)
        return True
    except (InvalidSignature, ValueError, OSError):
        return False


# ---------- events ----------

def record_event(db: Session, type_: str, *, message: Message | None = None, lead_id: int | None = None,
                 occurred_at: datetime | None = None, source_id: str | None = None, detail: str = "",
                 raw: dict | None = None) -> bool:
    """Store one event. Returns False if this source_id was already stored (SNS redelivery)."""
    new_id = db.scalar(insert(Event).values(
        message_id=message.id if message else None,
        lead_id=message.lead_id if message else lead_id,
        sender_id=message.sender_id if message else None,
        campaign_id=message.campaign_id if message else None,
        type=type_, occurred_at=occurred_at or datetime.now(UTC), source_id=source_id,
        detail=detail[:2000], raw=raw or {},
    ).on_conflict_do_nothing(index_elements=["source_id"]).returning(Event.id))
    return new_id is not None


def handle_bounce(db: Session, email: str, permanent: bool, message: Message | None, when: datetime,
                  source_id: str, detail: str, raw: dict) -> None:
    lead = db.scalar(select(Lead).where(Lead.email == email))
    if not record_event(db, "bounce_hard" if permanent else "bounce_soft", message=message,
                        lead_id=lead.id if lead else None, occurred_at=when, source_id=source_id,
                        detail=detail, raw=raw):
        return
    if permanent:
        suppress(db, email, "email", "hard_bounce", "bounce")
    elif lead is not None:
        lead.soft_bounce_count += 1
        if lead.soft_bounce_count >= SOFT_BOUNCES_TO_HARD:
            suppress(db, email, "email", "hard_bounce", f"{SOFT_BOUNCES_TO_HARD} soft bounces")


def handle_complaint(db: Session, email: str, message: Message | None, when: datetime, source_id: str,
                     raw: dict) -> None:
    lead = db.scalar(select(Lead).where(Lead.email == email))
    if record_event(db, "complaint", message=message, lead_id=lead.id if lead else None, occurred_at=when,
                    source_id=source_id, detail="Marked as spam", raw=raw):
        suppress(db, email, "email", "complaint", "spam complaint")


def parse_time(value: str | None) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else datetime.now(UTC)
    except ValueError:
        return datetime.now(UTC)


def handle_ses_event(db: Session, event: dict, source_id: str) -> str:
    """One SES event (configuration-set event or classic notification). Caller commits."""
    kind = event.get("eventType") or event.get("notificationType") or ""
    mail = event.get("mail", {})
    message = None
    if mail.get("messageId"):
        message = db.scalar(select(Message).where(Message.provider_message_id == mail["messageId"]))

    if kind == "Delivery":
        record_event(db, "delivery", message=message, occurred_at=parse_time(event.get("delivery", {}).get("timestamp")),
                     source_id=source_id, raw=event)
    elif kind == "Bounce":
        bounce = event.get("bounce", {})
        permanent = bounce.get("bounceType") == "Permanent"
        for r in bounce.get("bouncedRecipients", []):
            email = r.get("emailAddress", "").lower()
            detail = f"{bounce.get('bounceType')}/{bounce.get('bounceSubType')}: {r.get('diagnosticCode', '')}"
            handle_bounce(db, email, permanent, message, parse_time(bounce.get("timestamp")),
                          f"{source_id}:{email}", detail, event)
    elif kind == "Complaint":
        complaint = event.get("complaint", {})
        for r in complaint.get("complainedRecipients", []):
            email = r.get("emailAddress", "").lower()
            handle_complaint(db, email, message, parse_time(complaint.get("timestamp")), f"{source_id}:{email}", event)
    elif kind == "Reject":
        record_event(db, "reject", message=message, source_id=source_id,
                     detail=event.get("reject", {}).get("reason", ""), raw=event)
    else:
        return f"ignored {kind or 'unknown'} event"
    return f"handled {kind}"


def handle_sns(db: Session, payload: dict) -> str:
    """The SES webhook. Raises PermissionError for anything not signed by our SNS topic."""
    if not settings.sns_topic_arn or payload.get("TopicArn") != settings.sns_topic_arn:
        raise PermissionError("Unknown SNS topic")
    if not verify_sns(payload):
        raise PermissionError("Bad SNS signature")
    kind = payload.get("Type")
    if kind == "SubscriptionConfirmation":
        if not is_sns_url(payload.get("SubscribeURL", "")):
            raise PermissionError("Bad SubscribeURL")
        with urllib.request.urlopen(payload["SubscribeURL"], timeout=10):  # noqa: S310
            pass
        log.info("SNS subscription confirmed for %s", payload["TopicArn"])
        return "subscription confirmed"
    if kind != "Notification":
        return f"ignored {kind}"
    result = handle_ses_event(db, json.loads(payload["Message"]), payload["MessageId"])
    db.commit()
    return result


# ---------- health and auto-pause (core rule 6) ----------

def health(db: Session, now: datetime, *where) -> dict:
    """Bounce/complaint rate over the last 24 h of sends, or the last 200 sends if that is more."""
    sends = select(Message.id).where(Message.status.in_(("sent", "failed")), *where)
    last_day = db.scalar(select(func.count()).select_from(
        sends.where(Message.created_at >= now - timedelta(hours=24)).subquery()))
    if last_day >= HEALTH_WINDOW:
        window = sends.where(Message.created_at >= now - timedelta(hours=24))
    else:
        window = sends.order_by(Message.created_at.desc()).limit(HEALTH_WINDOW)
    ids = window.subquery()
    n = db.scalar(select(func.count()).select_from(ids))
    counts = dict(db.execute(
        select(Event.type, func.count(func.distinct(Event.message_id)))
        .where(Event.message_id.in_(select(ids.c.id)), Event.type.in_(("bounce_hard", "complaint")))
        .group_by(Event.type)).all())
    bounces, complaints = counts.get("bounce_hard", 0), counts.get("complaint", 0)
    return {
        "sends": n, "bounces": bounces, "complaints": complaints,
        "bounce_pct": 100 * bounces / n if n else 0.0,
        "complaint_pct": 100 * complaints / n if n else 0.0,
    }


def since(health_since: datetime | None) -> list:
    """Extra filter after a resume: only sends from then on are judged."""
    return [Message.created_at >= health_since] if health_since else []


def breach(stats: dict, s) -> str | None:
    """Reason text if the thresholds are crossed (only once there are enough sends to judge)."""
    if stats["sends"] < s.pause_min_sends:
        return None
    if stats["bounce_pct"] > s.bounce_pause_pct:
        return (f"Auto-paused: bounce rate {stats['bounce_pct']:.1f}% over the last {stats['sends']} sends "
                f"(limit {s.bounce_pause_pct:g}%)")
    if stats["complaint_pct"] > s.complaint_pause_pct:
        return (f"Auto-paused: complaint rate {stats['complaint_pct']:.2f}% over the last {stats['sends']} sends "
                f"(limit {s.complaint_pause_pct:g}%)")
    return None


def check_auto_pause(db: Session | None = None, now: datetime | None = None) -> list[str]:
    """Worker job (every minute): pause any sender or running campaign over the limits."""
    own = db is None
    db = db or SessionLocal()
    now = now or datetime.now(UTC)
    paused = []
    try:
        s = get_app_settings(db)
        for sender in db.scalars(select(Sender).where(Sender.status == "active")).all():
            reason = breach(health(db, now, Message.sender_id == sender.id, *since(sender.health_since)), s)
            if reason:
                sender.status, sender.pause_reason = "paused", reason[:255]
                paused.append(f"Sender {sender.from_email}: {reason}")
                audit(db, None, None, "sender_auto_paused", "sender", sender.id, reason=reason)
        for campaign in db.scalars(select(Campaign).where(Campaign.status == "running")).all():
            reason = breach(health(db, now, Message.campaign_id == campaign.id, *since(campaign.health_since)), s)
            if reason:
                campaign.status, campaign.pause_reason = "paused", reason[:255]
                paused.append(f"Campaign \"{campaign.name}\": {reason}")
                audit(db, None, None, "campaign_auto_paused", "campaign", campaign.id, reason=reason)
        db.commit()
        for line in paused:
            send_alert("Auto-paused", line + "\n\nClean the list before resuming.")
        return paused
    finally:
        if own:
            db.close()


def clear_old_raw(db: Session, now: datetime) -> int:
    """Retention: raw provider payloads are dropped after 90 days."""
    count = db.execute(update(Event).where(Event.occurred_at < now - timedelta(days=90), Event.raw != {})
                       .values(raw={})).rowcount
    db.commit()
    return count
