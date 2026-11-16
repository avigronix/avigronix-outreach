"""SPF / DKIM / DMARC checks for a sender's domain (SES identities are checked through the SES API)."""

import dns.exception
import dns.resolver
from botocore.exceptions import BotoCoreError, ClientError

from app.models import Sender
from app.services.providers import SendError, ses_client


def txt_records(name: str) -> list[str]:
    try:
        answers = dns.resolver.resolve(name, "TXT", lifetime=5)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return []
    except dns.exception.DNSException:
        return []
    return [b"".join(r.strings).decode(errors="replace") for r in answers]


def org_domain(domain: str) -> str:
    """mail.avigronix.com → avigronix.com (good enough for .com/.in/.io style domains)."""
    return ".".join(domain.split(".")[-2:])


def check_dmarc(domain: str) -> tuple[bool, str]:
    for name in dict.fromkeys([domain, org_domain(domain)]):
        record = next((t for t in txt_records(f"_dmarc.{name}") if t.lower().startswith("v=dmarc1")), None)
        if record:
            return True, f"_dmarc.{name}: {record}"
    return False, f"No DMARC record at _dmarc.{domain} or _dmarc.{org_domain(domain)}"


def check_spf(domain: str) -> tuple[bool, str]:
    record = next((t for t in txt_records(domain) if t.lower().startswith("v=spf1")), None)
    return (True, record) if record else (False, f"No SPF (v=spf1) TXT record on {domain}")


def check_dkim(domain: str, selector: str) -> tuple[bool, str]:
    if not selector:
        return False, "Enter the DKIM selector (Zoho shows it in Mail Admin → Domains → DKIM)"
    name = f"{selector}._domainkey.{domain}"
    record = next((t for t in txt_records(name) if "p=" in t), None)
    return (True, f"{name} found") if record else (False, f"No DKIM key at {name}")


def check_ses_identity(sender: Sender) -> dict[str, tuple[bool, str]]:
    """Ask SES about the domain identity (or the single address if only that is verified)."""
    try:
        client = ses_client()
    except SendError as exc:
        return {"SES identity": (False, str(exc))}
    for identity in (sender.domain, sender.from_email):
        try:
            info = client.get_email_identity(EmailIdentity=identity)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "NotFoundException":
                continue
            return {"SES identity": (False, str(exc))}
        except BotoCoreError as exc:
            return {"SES identity": (False, str(exc))}
        dkim = info.get("DkimAttributes", {}).get("Status", "NOT_STARTED")
        mail_from = info.get("MailFromAttributes", {})
        return {
            "SES identity": (info.get("VerifiedForSendingStatus", False), f"{identity} verified for sending"),
            "DKIM (Easy DKIM)": (dkim == "SUCCESS", f"status {dkim}"),
            "Custom MAIL FROM (SPF)": (
                mail_from.get("MailFromDomainStatus") == "SUCCESS",
                f"{mail_from.get('MailFromDomain') or 'not set'}: {mail_from.get('MailFromDomainStatus', 'not set')}",
            ),
        }
    return {"SES identity": (False, f"Neither {sender.domain} nor {sender.from_email} is an identity in SES")}


def check_sender_dns(sender: Sender) -> dict[str, list]:
    """{check name: [ok, detail]} — JSON-friendly for senders.dns_result."""
    if sender.type == "ses":
        results = check_ses_identity(sender)
    else:
        results = {"SPF": check_spf(sender.domain), "DKIM": check_dkim(sender.domain, sender.dkim_selector)}
    results["DMARC"] = check_dmarc(sender.domain)
    return {name: [bool(ok), detail] for name, (ok, detail) in results.items()}
