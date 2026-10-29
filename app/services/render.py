"""Turn a template + lead into the exact email that goes out (core rule 7 lives here).

Every email: footer (company + postal address + unsubscribe link), List-Unsubscribe +
List-Unsubscribe-Post headers, a unique Message-ID, Reply-To to the monitored mailbox.
Follow-ups in the same thread add In-Reply-To / References and a "Re:" subject.
"""

import html
import re
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

MERGE_FIELD = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*(?:\|([^}]*))?\}\}")

LEAD_FIELDS = ("first_name", "last_name", "company", "title", "city", "state", "country", "website",
               "industry", "email")

SAMPLE_LEAD = {
    "first_name": "Jason", "last_name": "Miller", "company": "BrightSmile Dental", "title": "Owner, DDS",
    "city": "Austin", "state": "TX", "country": "US", "website": "brightsmiledental.com", "industry": "Dental",
    "email": "jason@brightsmiledental.com", "custom": {},
}

SPAM_WORDS = (
    "for free", "free trial", "guarantee", "guaranteed", "100%", "act now", "limited time", "risk-free", "risk free",
    "no obligation", "winner", "cash", "earn money", "click here", "buy now", "urgent", "cheap",
    "best price", "special promotion", "once in a lifetime", "$$$", "double your", "increase sales",
)
SHORTENERS = ("bit.ly", "tinyurl.com", "goo.gl", "t.co", "ow.ly", "is.gd", "buff.ly", "rebrand.ly", "cutt.ly")
LINK = re.compile(r"(https?://[^\s<>\"')]+|www\.[^\s<>\"')]+)", re.I)


def lead_values(lead, sender_name: str = "") -> dict[str, str]:
    """Merge values from a Lead row or a plain dict (custom fields included)."""
    get = (lambda k: lead.get(k, "")) if isinstance(lead, dict) else (lambda k: getattr(lead, k, ""))
    values = {k: str(get(k) or "") for k in LEAD_FIELDS}
    values.update({k: str(v) for k, v in (get("custom") or {}).items()})
    values["sender_name"] = sender_name.split(" ")[0] if sender_name else ""
    return values


def merge(text: str, values: dict[str, str], escape: bool = False) -> tuple[str, list[str]]:
    """Fill {{field}} and {{field|fallback}}. Returns (text, fields that came out empty with no fallback)."""
    missing: list[str] = []

    def replace(m: re.Match) -> str:
        field, fallback = m.group(1), m.group(2)
        value = values.get(field, "").strip()
        if not value:
            if fallback is None:
                missing.append(field)
                return ""
            value = fallback.strip()
        return html.escape(value) if escape else value

    return MERGE_FIELD.sub(replace, text), missing


def lint(subjects: list[str], body_text: str, body_html: str = "") -> list[str]:
    """Deliverability warnings (FR-3.5, SRS 7.4). Warnings only, never block saving."""
    warnings = []
    body = body_text + "\n" + body_html
    links = {link.rstrip(".,") for link in LINK.findall(body)}
    if len(links) > 2:
        warnings.append(f"{len(links)} links — keep it to 1–2.")
    if any(s in link.lower() for link in links for s in SHORTENERS):
        warnings.append("Link shorteners (bit.ly etc.) hurt delivery. Use the full link.")
    images = len(re.findall(r"<img\b", body_html, re.I))
    if images:
        warnings.append(f"{images} image(s) in the HTML body — plain text delivers best.")
    words = len(MERGE_FIELD.sub("name", body_text).split())
    if words < 50 or words > 150:
        warnings.append(f"Body is {words} words — aim for 50–150.")
    found = sorted({w for w in SPAM_WORDS if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", " ".join(subjects + [body]), re.I)})
    if found:
        warnings.append("Spam-trigger words: " + ", ".join(found))
    for s in subjects:
        if s.isupper() and len(s) > 3:
            warnings.append(f'Subject in ALL CAPS: "{s}"')
        if "!" in s:
            warnings.append(f'Exclamation mark in subject: "{s}"')
    size_kb = len(body.encode()) / 1024
    if size_kb > 100:
        warnings.append(f"Email is {size_kb:.0f} KB — keep it under 100 KB.")
    if not body_text.strip():
        warnings.append("Plain-text body is empty.")
    return warnings


def footer_text(company: str, postal_address: str, unsubscribe_link: str) -> str:
    address = " · ".join(line.strip() for line in postal_address.splitlines() if line.strip())
    return f"\n\n--\n{company}" + (f" · {address}" if address else "") + \
        f"\nDon't want these emails? Unsubscribe: {unsubscribe_link}"


def footer_html(company: str, postal_address: str, unsubscribe_link: str) -> str:
    address = "<br>".join(html.escape(line.strip()) for line in postal_address.splitlines() if line.strip())
    return (
        '<hr style="border:0;border-top:1px solid #ddd;margin:24px 0 12px">'
        f'<p style="font-size:12px;color:#666">{html.escape(company)}' + (f"<br>{address}" if address else "") +
        f'<br>Don\'t want these emails? <a href="{html.escape(unsubscribe_link)}">Unsubscribe</a></p>'
    )


def render_content(subject: str, body_text: str, body_html: str, values: dict[str, str],
                   company: str, postal_address: str, unsubscribe_link: str) -> dict:
    """Merge fields + mandatory footer. Returns subject, text, html and the empty fields."""
    subject_out, m1 = merge(subject, values)
    text_out, m2 = merge(body_text, values)
    html_out, m3 = merge(body_html, values, escape=True) if body_html.strip() else ("", [])
    text_out = text_out.rstrip() + footer_text(company, postal_address, unsubscribe_link)
    if html_out:
        html_out = html_out + footer_html(company, postal_address, unsubscribe_link)
    return {
        "subject": " ".join(subject_out.split()),
        "text": text_out,
        "html": html_out,
        "missing": sorted(set(m1 + m2 + m3)),
    }


def build_message(
    *,
    from_name: str,
    from_email: str,
    to_email: str,
    reply_to: str,
    subject: str,
    text: str,
    html_body: str = "",
    unsubscribe_link: str,
    unsubscribe_mailto: str,
    in_reply_to: str | None = None,
    references: str | None = None,
    message_id: str | None = None,
) -> EmailMessage:
    """The MIME message both providers send."""
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_email))
    msg["To"] = to_email
    msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False, usegmt=True)
    msg["Message-ID"] = message_id or make_msgid(domain=from_email.rsplit("@", 1)[1])
    msg["List-Unsubscribe"] = f"<{unsubscribe_link}>, <mailto:{unsubscribe_mailto}?subject=unsubscribe>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return msg


def reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def compose(sender, to_email: str, subject: str, body_text: str, body_html: str, values: dict[str, str],
            company: str, postal_address: str, unsubscribe_link: str,
            in_reply_to: str | None = None, references: str | None = None) -> tuple[EmailMessage, dict]:
    """Render + build in one step. `sender` is a Sender row (from_name, from_email, reply_to)."""
    rendered = render_content(subject, body_text, body_html, values, company, postal_address, unsubscribe_link)
    msg = build_message(
        from_name=sender.from_name, from_email=sender.from_email, to_email=to_email,
        reply_to=sender.reply_to, subject=rendered["subject"], text=rendered["text"], html_body=rendered["html"],
        unsubscribe_link=unsubscribe_link, unsubscribe_mailto=sender.reply_to,
        in_reply_to=in_reply_to, references=references,
    )
    return msg, rendered
