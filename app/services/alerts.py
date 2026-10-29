"""Alert emails to the admin (ALERT_EMAIL), sent through the reply mailbox's SMTP login from .env."""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from app.config import settings

log = logging.getLogger("alerts")


def send_alert(subject: str, body: str) -> bool:
    """Best effort: an alert that can't be sent is logged, never raised."""
    log.warning("ALERT: %s — %s", subject, body)
    if not (settings.alert_email and settings.reply_email and settings.reply_smtp_host and settings.reply_password):
        log.warning("Alert email not sent: ALERT_EMAIL / REPLY_* SMTP settings are not complete in .env")
        return False
    msg = EmailMessage()
    msg["From"] = f"Outreach dashboard <{settings.reply_email}>"
    msg["To"] = settings.alert_email
    msg["Subject"] = f"[Outreach] {subject}"
    msg["Date"] = formatdate(usegmt=True)
    msg["Message-ID"] = make_msgid(domain=settings.reply_email.rsplit("@", 1)[1])
    msg.set_content(body + f"\n\nDashboard: {settings.base_url}")
    try:
        context = ssl.create_default_context()
        if settings.reply_smtp_port == 465:
            conn = smtplib.SMTP_SSL(settings.reply_smtp_host, 465, timeout=30, context=context)
        else:
            conn = smtplib.SMTP(settings.reply_smtp_host, settings.reply_smtp_port, timeout=30)
            conn.starttls(context=context)
        with conn:
            conn.login(settings.reply_email, settings.reply_password)
            conn.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log.error("Alert email failed: %s", exc)
        return False
