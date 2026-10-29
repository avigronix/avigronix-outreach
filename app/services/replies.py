"""Incoming mail (FR-7.4, FR-8.x): replies, out-of-office, unsubscribe requests and SMTP bounces (DSN),
plus answering a reply from the dashboard (FR-8.5).

Worker job poll_mailboxes(), every 3 minutes: the reply mailbox from .env plus every SMTP sender's own
mailbox. Mail is only read (never moved or deleted); a cursor per mailbox remembers the last IMAP UID.
"""

import html
import logging
import re
from datetime import date, datetime, timedelta, timezone

from imap_tools import AND, MailBox, MailMessage, UidRange
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import Campaign, Enrollment, ImapCursor, Lead, Message, Reply, Sender, User, get_app_settings
from app.services.crypto import decrypt_secret, unsubscribe_url
from app.services.events import handle_bounce, record_event
from app.services.providers import provider_for
from app.services.render import build_message, footer_text, reply_subject
from app.services.suppression import is_suppressed, stop_lead
from app.services.validation import is_free_mail

log = logging.getLogger("replies")

UTC = timezone.utc
MSGID = re.compile(r"<[^<>\s]+@[^<>\s]+>")
AUTO_SUBJECT = re.compile(
    r"(out of (the )?office|automatic reply|auto[- ]?reply|autoreply|auto[- ]?response|away from (the )?office|"
    r"on vacation|on leave|on holiday|abwesenheit|absence)", re.I)
UNSUBSCRIBE = re.compile(
    r"\b(unsubscribe|remove me|take me off|stop (emailing|sending|contacting)|opt[- ]?out|"
    r"do not (email|contact)|don'?t (email|contact) me)\b", re.I)
BOUNCE_SENDER = re.compile(r"^(mailer-daemon|postmaster|mail-daemon)@", re.I)
SMTP_STATUS = re.compile(r"\b([45])\.\d{1,3}\.\d{1,3}\b")
QUOTE_START = re.compile(r"^(On\b.*\bwrote:|-{2,}\s*Original Message\s*-{2,}|_{10,}|From:\s.+)$", re.I)
FIRST_POLL_DAYS = 7


# ---------- reading one email (pure) ----------

def header(obj, name: str) -> str:
    return str(obj.get(name, "") or "")


def is_auto_reply(obj, subject: str) -> bool:
    """FR-8.2: out-of-office and other automatic answers (they don't stop the sequence)."""
    auto = header(obj, "Auto-Submitted").strip().lower()
    if auto and auto != "no":
        return True
    if header(obj, "X-Autoreply") or header(obj, "X-Autorespond"):
        return True
    if header(obj, "Precedence").strip().lower() in ("auto_reply", "bulk", "junk"):
        return True
    return bool(AUTO_SUBJECT.search(subject))


def strip_quoted(text: str) -> str:
    """Keep only what the person wrote, not the quoted email below it."""
    kept = []
    for line in text.replace("\r\n", "\n").splitlines():
        stripped = line.strip()
        if stripped.startswith(">") or (kept and QUOTE_START.match(stripped)):
            break
        if stripped == "wrote:" and kept:  # Gmail sometimes wraps "On ... <x@y>\nwrote:"
            kept.pop()
            break
        kept.append(line)
    return "\n".join(kept).strip()[:20000]


def html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", value)
    return html.unescape(re.sub(r"<[^>]+>", " ", value))


def classify(obj, subject: str, body: str) -> str:
    if is_auto_reply(obj, subject):
        return "auto_reply"
    if UNSUBSCRIBE.search(subject + "\n" + body[:1500]):
        return "unsubscribe_request"
    return "reply"


def bounce_info(obj, from_email: str) -> dict | None:
    """A delivery failure report (DSN) or a mailer-daemon bounce: recipient, permanent?, detail, Message-IDs.
    None if this is not a bounce."""
    is_dsn = obj.get_content_type() == "multipart/report" and \
        (obj.get_param("report-type") or "").lower() == "delivery-status"
    if not is_dsn and not BOUNCE_SENDER.match(from_email):
        return None
    recipient, status, action = "", "", ""
    for part in obj.walk():
        if part.get_content_type() == "message/delivery-status":
            for fields in part.get_payload() or []:
                recipient = recipient or header(fields, "Final-Recipient").split(";")[-1].strip().lower()
                status = status or header(fields, "Status").strip()
                action = action or header(fields, "Action").strip().lower()
    if action == "delayed":
        return {"skip": True}
    whole = obj.as_string()
    if not status:
        found = SMTP_STATUS.search(whole)
        status = found.group(0) if found else ""
    return {
        "recipient": recipient,
        "permanent": status.startswith("5") or not status,  # a mailer-daemon failure with no code = hard
        "detail": f"{status or 'no status'} {action}".strip(),
        "message_ids": MSGID.findall(whole),
    }


# ---------- acting on it ----------

def find_message(db: Session, obj, from_email: str) -> Message | None:
    """FR-8.1: match by In-Reply-To / References, fall back to the sender's address."""
    ids = MSGID.findall(header(obj, "In-Reply-To") + " " + header(obj, "References"))
    if ids:
        message = db.scalar(select(Message).where(Message.message_id_header.in_(ids))
                            .order_by(Message.id.desc()).limit(1))
        if message is not None:
            return message
    return db.scalar(select(Message).join(Lead, Lead.id == Message.lead_id)
                     .where(Lead.email == from_email, Message.status == "sent")
                     .order_by(Message.id.desc()).limit(1))


def stop_on_reply(db: Session, lead: Lead, message: Message) -> int:
    """FR-8.3 / FR-4.4: the lead leaves the sequence; optionally everyone at the same company too.
    Returns how many other leads were stopped."""
    if lead.status in ("new", "in_sequence"):
        lead.status = "replied"
    stop_lead(db, lead.id, "replied")
    campaign = db.get(Campaign, message.campaign_id)
    if not campaign.stop_company_on_reply or is_free_mail(lead.domain):
        return 0
    colleagues = select(Lead.id).where(Lead.domain == lead.domain, Lead.id != lead.id)
    return db.execute(update(Enrollment).where(Enrollment.lead_id.in_(colleagues), Enrollment.state == "active")
                      .values(state="stopped", stop_reason="company replied", next_send_at=None)).rowcount


def process_mail(db: Session, mail: MailMessage, mailbox: str, own_addresses: set[str]) -> str:
    """Handle one incoming email. Returns what happened (for logs and tests). Commits."""
    obj = mail.obj
    from_email = (mail.from_ or "").strip().lower()
    subject = mail.subject or ""
    received = mail.date if mail.date and mail.date.tzinfo and mail.date.year > 2000 else datetime.now(UTC)
    if from_email in own_addresses:
        return "own"

    bounce = bounce_info(obj, from_email)
    if bounce is not None:
        if bounce.get("skip"):
            return "delayed"
        message = db.scalar(select(Message).where(Message.message_id_header.in_(bounce["message_ids"]))
                            .limit(1)) if bounce["message_ids"] else None
        recipient = bounce["recipient"] or (message.to_email if message else "")
        if not recipient:
            return "bounce without recipient"
        handle_bounce(db, recipient, bounce["permanent"], message, received,
                      f"dsn:{header(obj, 'Message-ID') or mailbox + ':' + str(mail.uid)}:{recipient}",
                      bounce["detail"], {"subject": subject, "from": from_email})
        db.commit()
        return "hard bounce" if bounce["permanent"] else "soft bounce"

    message = find_message(db, obj, from_email)
    if message is None:
        return "unmatched"
    message_id = header(obj, "Message-ID").strip() or f"<{mail.uid}@{mailbox}>"
    if db.scalar(select(Reply.id).where(Reply.message_id_header == message_id)):
        return "duplicate"
    lead = db.get(Lead, message.lead_id)
    body = strip_quoted(mail.text or html_to_text(mail.html or ""))
    classification = classify(obj, subject, body)
    db.add(Reply(
        message_id=message.id, lead_id=lead.id, mailbox=mailbox, message_id_header=message_id[:500],
        from_email=from_email, from_name=(mail.from_values.name if mail.from_values else "")[:255],
        subject=subject[:2000], body_text=body, received_at=received, classification=classification,
        assigned_to=lead.owner_id,
    ))
    record_event(db, "auto_reply" if classification == "auto_reply" else "reply", message=message,
                 occurred_at=received, source_id=f"reply:{message_id}"[:255], detail=body[:300])
    if classification != "auto_reply":
        stop_on_reply(db, lead, message)
    db.commit()
    return classification


# ---------- replying from the dashboard (FR-8.5) ----------

def quote(reply: Reply) -> str:
    when = reply.received_at.strftime("%a, %d %b %Y at %H:%M UTC")
    quoted = "\n".join("> " + line for line in reply.body_text.splitlines())
    return f"On {when}, {reply.from_name or reply.from_email} <{reply.from_email}> wrote:\n{quoted}"


def send_manual_reply(db: Session, incoming: Reply, user: User, text: str) -> Reply:
    """Answer an incoming reply through the SAME sender and provider that sent the original email.
    Raises ValueError (nothing sent) or SendError (provider refused). Commits on success."""
    text = text.strip()
    if not text:
        raise ValueError("Write a reply first.")
    message = db.get(Message, incoming.message_id) if incoming.message_id else None
    if message is None:
        raise ValueError("The original email is gone, so this can't be sent in the same thread. Reply from Zoho.")
    sender = db.get(Sender, message.sender_id)
    if sender.status == "disabled":
        raise ValueError(f"The sender {sender.from_email} is disabled. Reply from the mailbox instead.")
    if is_suppressed(db, incoming.from_email):
        raise ValueError(f"{incoming.from_email} is on the suppression list. Not sent.")

    s = get_app_settings(db)
    earlier = [m for m in (message.references.split() + [message.message_id_header, incoming.message_id_header])
               if m.startswith("<")]
    footer = footer_text(s.company_name, s.postal_address, unsubscribe_url(incoming.from_email))
    msg = build_message(
        from_name=sender.from_name, from_email=sender.from_email, to_email=incoming.from_email,
        reply_to=sender.reply_to, subject=reply_subject(incoming.subject or message.subject),
        text=f"{text}\n\n{quote(incoming)}{footer}", unsubscribe_link=unsubscribe_url(incoming.from_email),
        unsubscribe_mailto=sender.reply_to, in_reply_to=incoming.message_id_header,
        references=" ".join(dict.fromkeys(earlier)),
    )
    reply_box = settings.reply_email.lower()
    bcc = (reply_box,) if reply_box and reply_box not in (incoming.from_email, sender.from_email) else ()
    provider_for(sender).send(msg, bcc=bcc)  # SendError propagates; nothing is stored then

    sent = Reply(message_id=message.id, lead_id=incoming.lead_id, mailbox=sender.from_email,
                 message_id_header=msg["Message-ID"], from_email=sender.from_email, from_name=user.name,
                 subject=msg["Subject"], body_text=text, received_at=datetime.now(UTC), classification="sent",
                 direction="out", sent_by=user.id)
    db.add(sent)
    incoming.draft = ""
    if incoming.assigned_to is None:
        incoming.assigned_to = user.id
    db.commit()
    return sent


# ---------- inbox queries ----------

def visible(user: User):
    """Sales see replies assigned to them or from their own leads; admin/manager see all."""
    stmt = select(Reply).join(Lead, Lead.id == Reply.lead_id).where(Reply.direction == "in")
    if user.role == "sales":
        stmt = stmt.where(or_(Reply.assigned_to == user.id, Lead.owner_id == user.id))
    return stmt


def tab_filter(stmt, tab: str):
    real = Reply.classification != "auto_reply"
    if tab == "needs_action":
        return stmt.where(real, Reply.outcome.is_(None))
    if tab == "interested":
        return stmt.where(Reply.outcome.in_(("interested", "meeting_booked")))
    if tab == "auto":
        return stmt.where(Reply.classification == "auto_reply")
    return stmt.where(real)


def needs_action_count(db: Session, user: User) -> int:
    return db.scalar(select(func.count()).select_from(tab_filter(visible(user), "needs_action").subquery()))


# ---------- polling ----------

def mailboxes(db: Session) -> list[tuple[str, str, int, str, str]]:
    """(name, host, port, user, password) for every mailbox we read."""
    boxes = []
    if settings.reply_imap_host and settings.reply_email and settings.reply_password:
        boxes.append((settings.reply_email.lower(), settings.reply_imap_host, settings.reply_imap_port,
                      settings.reply_email, settings.reply_password))
    for s in db.scalars(select(Sender).where(Sender.type == "smtp", Sender.imap_host != "",
                                             Sender.status != "disabled")):
        if s.from_email not in {b[0] for b in boxes}:
            boxes.append((s.from_email, s.imap_host, s.imap_port, s.smtp_user or s.from_email,
                          decrypt_secret(s.smtp_password_enc)))
    return boxes


def poll_mailbox(db: Session, name: str, host: str, port: int, user: str, password: str,
                 own_addresses: set[str]) -> dict[str, int]:
    cursor = db.scalar(select(ImapCursor).where(ImapCursor.mailbox == name)) or ImapCursor(mailbox=name)
    db.add(cursor)
    results: dict[str, int] = {}
    with MailBox(host, port, timeout=30).login(user, password, initial_folder="INBOX") as box:
        uidvalidity = int(box.folder.status("INBOX", ["UIDVALIDITY"])["UIDVALIDITY"])
        if cursor.uidvalidity != uidvalidity:
            # First poll, or the mailbox was rebuilt: read the last week only, not years of history.
            cursor.uidvalidity, cursor.last_uid = uidvalidity, 0
            criteria = AND(date_gte=date.today() - timedelta(days=FIRST_POLL_DAYS))
        else:
            criteria = AND(uid=UidRange(cursor.last_uid + 1, "*"))
        db.commit()  # the cursor survives a rollback of one bad email below
        for mail in box.fetch(criteria, mark_seen=False, bulk=50):
            uid = int(mail.uid)
            if uid <= cursor.last_uid:
                continue  # "N:*" always returns the newest message, even when it is older than N
            try:
                outcome = process_mail(db, mail, name, own_addresses)
            except Exception:
                db.rollback()
                log.exception("Could not process mail %s in %s; skipping it", uid, name)
                outcome = "error"
            results[outcome] = results.get(outcome, 0) + 1
            cursor.last_uid = max(cursor.last_uid, uid)
            db.add(cursor)
            db.commit()
    cursor.last_polled_at, cursor.last_error = datetime.now(UTC), ""
    db.commit()
    return results


def poll_mailboxes() -> None:
    """Worker job: read every monitored mailbox once."""
    with SessionLocal() as db:
        own = {settings.reply_email.lower()} | set(db.scalars(select(Sender.from_email)))
        for name, host, port, user, password in mailboxes(db):
            try:
                results = poll_mailbox(db, name, host, port, user, password, own)
                if results:
                    log.info("Mailbox %s: %s", name, results)
            except Exception as exc:
                db.rollback()
                log.error("Mailbox %s could not be read: %s", name, exc)
                cursor = db.scalar(select(ImapCursor).where(ImapCursor.mailbox == name)) or ImapCursor(mailbox=name)
                cursor.last_error, cursor.last_polled_at = str(exc)[:500], datetime.now(UTC)
                db.add(cursor)
                db.commit()
