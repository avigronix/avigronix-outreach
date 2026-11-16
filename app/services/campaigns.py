"""Campaign segment, pre-launch checklist (FR-4.6), enrolment and state changes."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    Campaign, CampaignSender, Enrollment, Lead, Message, Sender, SequenceStep, Suppression, Template,
    get_app_settings,
)
from app.services.scheduler import parse_hm

MAX_STEPS = 5


def segment_base(campaign: Campaign):
    """New leads with at least one of the campaign's tags (and one of its countries, if set)."""
    stmt = select(Lead.id).where(Lead.status == "new", Lead.tags.overlap(campaign.segment.get("tags") or ["-"]))
    countries = [c.upper() for c in campaign.segment.get("countries") or []]
    if countries:
        stmt = stmt.where(func.upper(Lead.country).in_(countries))
    return stmt


def exclusion_rules(campaign: Campaign) -> dict:
    """Why a matching lead is not enrolled. Each value is a SQL condition on Lead."""
    rules = {
        "suppressed": exists(select(Suppression.id).where(
            or_(Suppression.value == Lead.email, Suppression.value == Lead.domain))),
        "in another campaign": exists(select(Enrollment.id).where(
            Enrollment.lead_id == Lead.id, Enrollment.state == "active", Enrollment.campaign_id != campaign.id)),
        "already in this campaign": exists(select(Enrollment.id).where(
            Enrollment.lead_id == Lead.id, Enrollment.campaign_id == campaign.id)),
    }
    if campaign.provider == "ses":
        rules["purchased list (SMTP only)"] = Lead.source_type == "purchased"
    if not campaign.allow_canada:
        rules["Canada (CASL)"] = func.upper(Lead.country) == "CA"
    return rules


def eligible_leads(campaign: Campaign):
    stmt = segment_base(campaign)
    for condition in exclusion_rules(campaign).values():
        stmt = stmt.where(~condition)
    return stmt


def segment_summary(db: Session, campaign: Campaign) -> dict:
    count = lambda stmt: db.scalar(select(func.count()).select_from(stmt.subquery()))  # noqa: E731
    base = segment_base(campaign)
    return {
        "matching": count(base),
        "eligible": count(eligible_leads(campaign)),
        "excluded": {name: count(base.where(cond)) for name, cond in exclusion_rules(campaign).items()},
    }


def steps_of(db: Session, campaign: Campaign) -> list[SequenceStep]:
    return db.scalars(select(SequenceStep).where(SequenceStep.campaign_id == campaign.id)
                      .order_by(SequenceStep.step_no)).all()


def pool_of(db: Session, campaign: Campaign) -> list[Sender]:
    return db.scalars(select(Sender).join(CampaignSender, CampaignSender.sender_id == Sender.id)
                      .where(CampaignSender.campaign_id == campaign.id).order_by(Sender.from_email)).all()


def preflight(db: Session, campaign: Campaign) -> list[tuple[str, bool, str]]:
    """FR-4.6 checklist: (label, ok, detail). A campaign starts only when every line is ok."""
    checks = []
    steps = steps_of(db, campaign)
    templates = [db.get(Template, s.template_id) for s in steps]
    checks.append(("Sequence", bool(steps) and steps[0].step_no == 1 and all(templates),
                   f"{len(steps)} step(s)" if steps else "Add at least the initial email"))

    pool = [s for s in pool_of(db, campaign) if s.status == "active" and s.type == campaign.provider]
    checks.append(("Active sender in pool", bool(pool),
                   ", ".join(s.from_email for s in pool) or f"Add an active {campaign.provider.upper()} sender"))
    unverified = [s.from_email for s in pool if not s.dns_ok]
    checks.append(("Sender DNS verified (SPF · DKIM · DMARC)", bool(pool) and not unverified,
                   "Run the DNS check on: " + ", ".join(unverified) if unverified else "All pass"))

    address = get_app_settings(db).postal_address.strip()
    checks.append(("Footer with postal address", bool(address), address.splitlines()[0] if address else
                   "Add the postal address on the Settings page"))
    checks.append(("One-click unsubscribe header", True, "Added to every email"))

    untested = [t.name for t in templates if t and not t.test_sent_at]
    checks.append(("Test email sent", bool(templates) and not untested,
                   "Send a test of: " + ", ".join(untested) if untested else "All templates tested"))

    windows_ok = bool(campaign.schedule.get("windows")) and bool(campaign.schedule.get("weekdays"))
    checks.append(("Send window", windows_ok, "OK" if windows_ok else "Add a time window and at least one weekday"))

    if campaign.status == "draft":
        summary = segment_summary(db, campaign)
        removed = summary["excluded"]["suppressed"]
        checks.append(("Segment has sendable leads", summary["eligible"] > 0,
                       f"{summary['eligible']:,} ready of {summary['matching']:,} matching"))
        checks.append(("Suppressed leads excluded", True, f"{removed:,} removed"))
    return checks


def enroll(db: Session, campaign: Campaign, now: datetime) -> int:
    """Add every eligible lead to the campaign. Returns how many were added. Caller commits."""
    start = max(now, campaign.start_at or now)
    lead_ids = db.scalars(eligible_leads(campaign)).all()
    added = 0
    for i in range(0, len(lead_ids), 1000):
        rows = [{"campaign_id": campaign.id, "lead_id": lead_id, "current_step": 1, "next_send_at": start,
                 "state": "active", "stop_reason": ""} for lead_id in lead_ids[i : i + 1000]]
        # ON CONFLICT also covers "already active in another campaign" (partial unique index).
        added += len(db.execute(insert(Enrollment).values(rows).on_conflict_do_nothing()
                                .returning(Enrollment.id)).all())
    return added


def start(db: Session, campaign: Campaign) -> int:
    now = datetime.now(timezone.utc)
    campaign.status = "scheduled" if campaign.start_at and campaign.start_at > now else "running"
    campaign.started_at = campaign.started_at or now
    campaign.pause_reason = ""
    return enroll(db, campaign, now)


def stop(db: Session, campaign: Campaign) -> None:
    campaign.status = "stopped"
    db.execute(update(Enrollment).where(Enrollment.campaign_id == campaign.id, Enrollment.state == "active")
               .values(state="stopped", stop_reason="campaign stopped", next_send_at=None))
    db.execute(update(Message).where(Message.campaign_id == campaign.id, Message.status == "retry")
               .values(status="failed", error="Campaign stopped before retry"))


def validate_schedule(windows_raw: str, weekdays: list[int], default_tz: str) -> tuple[dict | None, str | None]:
    """'09:00-11:30, 13:30-16:30' → schedule dict, or an error message."""
    windows = []
    for part in windows_raw.split(","):
        if not part.strip():
            continue
        try:
            a, b = (x.strip() for x in part.split("-"))
            if parse_hm(a) >= parse_hm(b):
                raise ValueError
        except ValueError:
            return None, f"Time window '{part.strip()}' must look like 09:00-11:30."
        windows.append([a, b])
    if not windows:
        return None, "Add at least one time window, e.g. 09:00-11:30."
    if not weekdays:
        return None, "Pick at least one weekday."
    try:
        ZoneInfo(default_tz)
    except (ZoneInfoNotFoundError, ValueError):
        return None, f"Unknown timezone {default_tz}."
    return {"windows": windows, "weekdays": sorted(set(weekdays)), "default_tz": default_tz}, None
