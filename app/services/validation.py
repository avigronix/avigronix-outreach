"""Email checks, MX lookups and timezone resolution used by the importer."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import dns.exception
import dns.resolver
from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import MxCache

ROLE_PREFIXES = {
    "info", "sales", "admin", "office", "contact", "support", "hello", "team", "billing",
    "accounts", "accounting", "hr", "jobs", "careers", "marketing", "noreply", "no-reply",
    "webmaster", "postmaster", "enquiries", "enquiry", "inquiries", "help", "reception",
    "frontdesk", "mail", "service", "customerservice",
}

FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in", "ymail.com",
    "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "msn.com", "aol.com",
    "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com", "gmx.com", "gmx.de",
    "mail.com", "zoho.com", "yandex.com", "yandex.ru", "rediffmail.com", "comcast.net",
    "att.net", "verizon.net", "sbcglobal.net", "bellsouth.net", "cox.net",
}

DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "sharklasers.com",
    "10minutemail.com", "10minutemail.net", "tempmail.com", "temp-mail.org", "temp-mail.io",
    "yopmail.com", "yopmail.net", "trashmail.com", "getnada.com", "nada.email",
    "dispostable.com", "maildrop.cc", "throwawaymail.com", "fakeinbox.com", "mintemail.com",
    "mohmal.com", "emailondeck.com", "spamgourmet.com", "mailnesia.com", "tempail.com",
    "moakt.com", "burnermail.io", "33mail.com", "inboxkitten.com", "mailcatch.com",
    "spambox.us", "tempinbox.com", "discard.email", "mytemp.email", "tmpmail.org",
}

US_STATE_TZ = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix",
    "AR": "America/Chicago", "CA": "America/Los_Angeles", "CO": "America/Denver",
    "CT": "America/New_York", "DE": "America/New_York", "DC": "America/New_York",
    "FL": "America/New_York", "GA": "America/New_York", "HI": "Pacific/Honolulu",
    "ID": "America/Boise", "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis",
    "IA": "America/Chicago", "KS": "America/Chicago", "KY": "America/New_York",
    "LA": "America/Chicago", "ME": "America/New_York", "MD": "America/New_York",
    "MA": "America/New_York", "MI": "America/Detroit", "MN": "America/Chicago",
    "MS": "America/Chicago", "MO": "America/Chicago", "MT": "America/Denver",
    "NE": "America/Chicago", "NV": "America/Los_Angeles", "NH": "America/New_York",
    "NJ": "America/New_York", "NM": "America/Denver", "NY": "America/New_York",
    "NC": "America/New_York", "ND": "America/Chicago", "OH": "America/New_York",
    "OK": "America/Chicago", "OR": "America/Los_Angeles", "PA": "America/New_York",
    "RI": "America/New_York", "SC": "America/New_York", "SD": "America/Chicago",
    "TN": "America/Chicago", "TX": "America/Chicago", "UT": "America/Denver",
    "VT": "America/New_York", "VA": "America/New_York", "WA": "America/Los_Angeles",
    "WV": "America/New_York", "WI": "America/Chicago", "WY": "America/Denver",
    "PR": "America/Puerto_Rico",
}

US_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "washington dc": "DC", "washington d.c.": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "puerto rico": "PR",
}

COUNTRY_ALIASES = {
    "us": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US", "united states": "US",
    "united states of america": "US", "america": "US",
    "uk": "GB", "gb": "GB", "united kingdom": "GB", "great britain": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "ca": "CA", "canada": "CA", "ie": "IE", "ireland": "IE", "au": "AU", "australia": "AU",
    "nz": "NZ", "new zealand": "NZ", "in": "IN", "india": "IN", "de": "DE", "germany": "DE",
    "fr": "FR", "france": "FR", "nl": "NL", "netherlands": "NL", "es": "ES", "spain": "ES",
    "it": "IT", "italy": "IT", "se": "SE", "sweden": "SE", "ch": "CH", "switzerland": "CH",
    "ae": "AE", "uae": "AE", "united arab emirates": "AE", "sg": "SG", "singapore": "SG",
    "za": "ZA", "south africa": "ZA", "be": "BE", "belgium": "BE", "dk": "DK", "denmark": "DK",
    "no": "NO", "norway": "NO", "fi": "FI", "finland": "FI", "pt": "PT", "portugal": "PT",
    "pl": "PL", "poland": "PL", "at": "AT", "austria": "AT", "il": "IL", "israel": "IL",
}

COUNTRY_TZ = {
    "GB": "Europe/London", "IE": "Europe/Dublin", "CA": "America/Toronto",
    "AU": "Australia/Sydney", "NZ": "Pacific/Auckland", "IN": "Asia/Kolkata",
    "DE": "Europe/Berlin", "FR": "Europe/Paris", "NL": "Europe/Amsterdam",
    "ES": "Europe/Madrid", "IT": "Europe/Rome", "SE": "Europe/Stockholm",
    "CH": "Europe/Zurich", "AE": "Asia/Dubai", "SG": "Asia/Singapore",
    "ZA": "Africa/Johannesburg", "BE": "Europe/Brussels", "DK": "Europe/Copenhagen",
    "NO": "Europe/Oslo", "FI": "Europe/Helsinki", "PT": "Europe/Lisbon",
    "PL": "Europe/Warsaw", "AT": "Europe/Vienna", "IL": "Asia/Jerusalem",
}

TZ_ABBREVIATIONS = {
    "EST": "America/New_York", "EDT": "America/New_York", "ET": "America/New_York",
    "EASTERN": "America/New_York", "CST": "America/Chicago", "CDT": "America/Chicago",
    "CT": "America/Chicago", "CENTRAL": "America/Chicago", "MST": "America/Denver",
    "MDT": "America/Denver", "MT": "America/Denver", "MOUNTAIN": "America/Denver",
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles", "PT": "America/Los_Angeles",
    "PACIFIC": "America/Los_Angeles", "AKST": "America/Anchorage", "HST": "Pacific/Honolulu",
    "GMT": "Europe/London", "BST": "Europe/London", "IST": "Asia/Kolkata",
}

US_DEFAULT_TZ = "America/New_York"
MX_CACHE_DAYS = 30


def normalize_email(raw: str) -> str | None:
    """Lower-cased email if the syntax is valid, else None."""
    try:
        return validate_email(raw.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return None


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[1]


def is_role_address(email: str) -> bool:
    local = email.split("@", 1)[0]
    return local in ROLE_PREFIXES or local.split("+", 1)[0] in ROLE_PREFIXES


def is_free_mail(domain: str) -> bool:
    return domain in FREE_MAIL_DOMAINS


def is_disposable(domain: str) -> bool:
    return domain in DISPOSABLE_DOMAINS


def normalize_country(raw: str) -> str:
    """'USA' / 'United States' → 'US'. Unknown values are kept as typed."""
    value = raw.strip()
    return COUNTRY_ALIASES.get(value.lower(), value.upper() if len(value) == 2 else value)


def normalize_us_state(raw: str) -> str:
    """'Texas' / 'tx' → 'TX'. Unknown values are kept as typed."""
    value = raw.strip()
    if value.upper() in US_STATE_TZ:
        return value.upper()
    return US_STATE_NAMES.get(value.lower(), value)


def valid_timezone(name: str) -> str | None:
    name = name.strip()
    if not name:
        return None
    if name.upper() in TZ_ABBREVIATIONS:
        return TZ_ABBREVIATIONS[name.upper()]
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError):
        return None


def resolve_timezone(tz_value: str, state: str, country: str) -> tuple[str | None, bool]:
    """FR-2.9: timezone column → US state → country → America/New_York for US → None
    (campaign default at send time). Returns (timezone, found) where found=False means a fallback."""
    tz = valid_timezone(tz_value)
    if tz:
        return tz, True
    is_us = country in ("US", "")
    if is_us and state in US_STATE_TZ:
        return US_STATE_TZ[state], True
    if country in COUNTRY_TZ:
        return COUNTRY_TZ[country], True
    if country == "US":
        return US_DEFAULT_TZ, False
    return None, False


def lookup_mx(domain: str) -> bool | None:
    """True = has MX, False = no MX / domain doesn't exist / null MX, None = DNS error (don't reject)."""
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return False
    except dns.exception.DNSException:
        return None
    hosts = [str(r.exchange).rstrip(".") for r in answers]
    return any(hosts)  # a single "." is a null MX: the domain accepts no mail


def check_mx_domains(db: Session, domains: set[str]) -> dict[str, bool | None]:
    """MX result per domain, using mx_cache (30 days) and parallel lookups for the rest."""
    fresh_after = datetime.now(timezone.utc) - timedelta(days=MX_CACHE_DAYS)
    result: dict[str, bool | None] = {}
    domain_list = list(domains)
    for i in range(0, len(domain_list), 1000):
        rows = db.execute(
            select(MxCache.domain, MxCache.has_mx).where(
                MxCache.domain.in_(domain_list[i : i + 1000]), MxCache.checked_at >= fresh_after
            )
        )
        result.update({d: has for d, has in rows})

    missing = [d for d in domains if d not in result]
    with ThreadPoolExecutor(max_workers=20) as pool:
        looked_up = dict(zip(missing, pool.map(lookup_mx, missing)))
    result.update(looked_up)

    now = datetime.now(timezone.utc)
    to_cache = [{"domain": d, "has_mx": has, "checked_at": now} for d, has in looked_up.items() if has is not None]
    for i in range(0, len(to_cache), 1000):
        stmt = insert(MxCache).values(to_cache[i : i + 1000])
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=["domain"], set_={"has_mx": stmt.excluded.has_mx, "checked_at": now}
            )
        )
    db.commit()
    return result
