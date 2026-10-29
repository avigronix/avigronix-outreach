"""The two ways an email leaves the system. Both have one method: send(msg, bcc=()) -> provider_message_id.
BCC addresses go only into the envelope, never into the message headers.

SendError.kind tells the scheduler what happened:
  "temporary" → nothing was sent; retry later (throttling, 4xx, can't connect)
  "sender"    → nothing was sent; the sender itself is broken (login, keys, unverified, account paused)
                → pause the sender, the email waits
  "recipient" → this address is rejected for good (5xx user unknown) → treat as hard bounce
  "message"   → this message is rejected for good (content, other 5xx)
Any other exception → we don't know if it went out; the scheduler must NOT resend.
"""

import smtplib
import socket
import ssl
from email.message import EmailMessage

import boto3
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

from app.config import settings
from app.models import Sender
from app.services.crypto import decrypt_secret

SES_TEMPORARY_CODES = {"TooManyRequestsException", "LimitExceededException", "Throttling",
                       "ThrottlingException", "InternalFailure", "ServiceUnavailable"}
SES_SENDER_CODES = {"MailFromDomainNotVerifiedException", "AccountSuspendedException", "SendingPausedException",
                    "NotFoundException", "AccessDeniedException", "UnrecognizedClientException",
                    "InvalidClientTokenId", "SignatureDoesNotMatch", "ExpiredTokenException"}
SMTP_TIMEOUT = 30


class SendError(Exception):
    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind

    @property
    def temporary(self) -> bool:
        return self.kind == "temporary"


def ses_client():
    if not (settings.aws_access_key_id and settings.aws_secret_access_key):
        raise SendError("AWS keys are not set in .env.", "sender")
    return boto3.client(
        "sesv2",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )


class SesProvider:
    def __init__(self, sender: Sender):
        self.sender = sender

    def send(self, msg: EmailMessage, bcc: tuple[str, ...] = ()) -> str:
        client = ses_client()
        destination = {"ToAddresses": [msg["To"]]}
        if bcc:
            destination["BccAddresses"] = list(bcc)
        kwargs = {
            "FromEmailAddress": self.sender.from_email,
            "Destination": destination,
            "Content": {"Raw": {"Data": msg.as_bytes()}},
        }
        if settings.ses_configuration_set:
            kwargs["ConfigurationSetName"] = settings.ses_configuration_set
        try:
            return client.send_email(**kwargs)["MessageId"]
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            kind = "temporary" if code in SES_TEMPORARY_CODES else "sender" if code in SES_SENDER_CODES else "message"
            raise SendError(f"SES {code}: {exc}", kind)
        except EndpointConnectionError as exc:
            raise SendError(f"SES unreachable: {exc}", "temporary")
        except BotoCoreError as exc:
            raise SendError(f"SES error: {exc}", "sender")


def _kind_for_code(code: int, permanent_kind: str) -> str:
    return "temporary" if 400 <= code < 500 else permanent_kind


class SmtpProvider:
    def __init__(self, sender: Sender):
        self.sender = sender

    def _connect(self) -> smtplib.SMTP:
        s = self.sender
        try:
            password = decrypt_secret(s.smtp_password_enc)
        except RuntimeError as exc:
            raise SendError(str(exc), "sender")
        context = ssl.create_default_context()
        if s.smtp_port == 465:
            conn = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=SMTP_TIMEOUT, context=context)
        else:
            conn = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=SMTP_TIMEOUT)
            conn.starttls(context=context)
        conn.login(s.smtp_user or s.from_email, password)
        return conn

    def send(self, msg: EmailMessage, bcc: tuple[str, ...] = ()) -> str:
        # Connecting and logging in happen before anything is sent: any failure there is safe to retry.
        try:
            conn = self._connect()
        except smtplib.SMTPAuthenticationError as exc:
            raise SendError(f"SMTP login failed: {exc.smtp_code} {exc.smtp_error!r}", "sender")
        except (smtplib.SMTPException, OSError, socket.timeout) as exc:
            raise SendError(f"SMTP connect failed: {exc}", "temporary")

        try:
            conn.send_message(msg, to_addrs=[msg["To"], *bcc])
        except smtplib.SMTPRecipientsRefused as exc:
            code = next(iter(exc.recipients.values()))[0]
            raise SendError(f"Recipient refused: {exc.recipients}", _kind_for_code(code, "recipient"))
        except smtplib.SMTPSenderRefused as exc:
            raise SendError(f"Sender refused {exc.smtp_code}: {exc.smtp_error!r}", _kind_for_code(exc.smtp_code, "sender"))
        except smtplib.SMTPDataError as exc:
            raise SendError(f"SMTP {exc.smtp_code}: {exc.smtp_error!r}", _kind_for_code(exc.smtp_code, "message"))
        # Disconnects/timeouts during DATA propagate: the result is unknown.
        finally:
            try:
                conn.quit()
            except Exception:
                pass
        return msg["Message-ID"]


def provider_for(sender: Sender):
    return SesProvider(sender) if sender.type == "ses" else SmtpProvider(sender)
