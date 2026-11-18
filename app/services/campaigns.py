"""Campaign segment, pre-launch checklist (FR-4.6), enrolment and state changes."""

import heapq
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    Campaign, CampaignSender, Enrollment, Lead, Message, Sender, SequenceStep, Suppression, Template,
    get_app_settings,
)
from app.services.scheduler import add_business_days, is_send_day, parse_hm
from app.services.warmup import MIN_GAP_SEC, warmup_cap

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


def preflight(db: Session, campaign: Campaign) -> list[tuple[str, bool, str, str]]:
    """FR-4.6 checklist: (label, ok, detail, page where it is fixed or ""). A campaign starts only when every
    line is ok."""
    checks = []
    edit = f"/campaigns/{campaign.id}/edit"
    steps = steps_of(db, campaign)
    templates = [db.get(Template, s.template_id) for s in steps]
    checks.append(("Sequence", bool(steps) and steps[0].step_no == 1 and all(templates),
                   f"{len(steps)} step(s)" if steps else "Add at least the initial email", edit))

    pool = [s for s in pool_of(db, campaign) if s.status == "active" and s.type == campaign.provider]
    checks.append(("Active sender in pool", bool(pool),
                   ", ".join(s.from_email for s in pool) or f"Add an active {campaign.provider.upper()} sender", edit))
    unverified = [s for s in pool if not s.dns_ok]
    checks.append(("Sender DNS verified (SPF · DKIM · DMARC)", bool(pool) and not unverified,
                   "Run the DNS check on: " + ", ".join(s.from_email for s in unverified) if unverified else "All pass",
                   f"/senders/{unverified[0].id}" if unverified else "/senders"))

    address = get_app_settings(db).postal_address.strip()
    checks.append(("Footer with postal address", bool(address), address.splitlines()[0] if address else
                   "Add the postal address on the Settings page", "/settings"))
    checks.append(("One-click unsubscribe header", True, "Added to every email", ""))

    untested = [t for t in templates if t and not t.test_sent_at]
    checks.append(("Test email sent", bool(templates) and not untested,
                   "Send a test of: " + ", ".join(t.name for t in untested) if untested else "All templates tested",
                   f"/templates/{untested[0].id}" if untested else edit))

    windows_ok = bool(campaign.schedule.get("windows")) and bool(campaign.schedule.get("weekdays"))
    checks.append(("Send window", windows_ok, "OK" if windows_ok else "Add a time window and at least one weekday",
                   edit))

    if campaign.status == "draft":
        summary = segment_summary(db, campaign)
        removed = summary["excluded"]["suppressed"]
        checks.append(("Segment has sendable leads", summary["eligible"] > 0,
                       f"{summary['eligible']:,} ready of {summary['matching']:,} matching", edit))
        checks.append(("Suppressed leads excluded", True, f"{removed:,} removed", ""))
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


# ---------- send plan (the "what will happen if I start" estimate) ----------

PLAN_HORIZON_DAYS = 365


def send_plan(leads: list[tuple[str, bool, int, date]], *, start: date, weekdays: list[int], delays: dict[int, int],
              campaign_cap: int, senders: list[dict], domain_max: int) -> tuple[list[tuple[date, dict[int, int]]], int]:
    """Day-by-day estimate of a campaign's sends, assuming nobody replies, bounces or unsubscribes (the most it
    will ever send). Uses the scheduler's own rules: send days + US holidays, campaign cap, each sender's warm-up
    and daily cap (warm-up rising one day after each sending day), the per-domain limit, follow-up delays.

    leads: (domain, free_mail, next step_no, first possible day) per lead still to email.
    senders: {"type", "warmup_day", "override", "daily_cap", "gap_limit"} per active pool sender.
    Returns ([(day, {step_no: emails})] for every day that sends, emails still unsent after a year)."""
    queue = [(due, n, step, domain, free) for n, (domain, free, step, due) in enumerate(leads)]
    heapq.heapify(queue)
    seq, warm, plan = len(queue), [s["warmup_day"] for s in senders], []
    day, last_day = start, start + timedelta(days=PLAN_HORIZON_DAYS)
    while queue and day <= last_day:
        if is_send_day(day, weekdays):
            cap = min(campaign_cap, sum(min(s["override"] or warmup_cap(s["type"], w), s["daily_cap"], s["gap_limit"])
                                        for s, w in zip(senders, warm)))
            sent: dict[int, int] = {}
            per_domain: dict[str, int] = {}
            waiting = []  # over the per-domain limit today: tomorrow
            while queue and queue[0][0] <= day and sum(sent.values()) < cap:
                due, n, step, domain, free = heapq.heappop(queue)
                if not free and per_domain.get(domain, 0) >= domain_max:
                    waiting.append((day + timedelta(days=1), n, step, domain, free))
                    continue
                per_domain[domain] = per_domain.get(domain, 0) + 1
                sent[step] = sent.get(step, 0) + 1
                if step + 1 in delays:
                    seq += 1
                    heapq.heappush(queue, (add_business_days(day, delays[step + 1]), seq, step + 1, domain, free))
            for item in waiting:
                heapq.heappush(queue, item)
            if sent:
                plan.append((day, sent))
                warm = [w + 1 for w in warm]
        day += timedelta(days=1)
    unsent = sum(1 + sum(1 for s in delays if s > step) for _, _, step, _, _ in queue)
    return plan, unsent


def plan_for(db: Session, campaign: Campaign, now: datetime) -> dict:
    """send_plan() for a campaign: from today (or its start date), its leads still to email, its pool."""
    s = get_app_settings(db)
    tz = ZoneInfo(s.cap_day_tz)
    today = now.astimezone(tz).date()
    steps = steps_of(db, campaign)
    if not steps:
        return {"problem": "Add the sequence first."}
    pool = [x for x in pool_of(db, campaign) if x.status == "active" and x.type == campaign.provider]
    if not pool:
        return {"problem": f"No active {campaign.provider.upper()} sender in the pool, so nothing would go out."}
    window_seconds = sum((datetime.combine(today, parse_hm(b)) - datetime.combine(today, parse_hm(a))).seconds
                         for a, b in campaign.schedule.get("windows", []))
    senders = [{"type": x.type, "warmup_day": x.warmup_day, "override": x.warmup_override_cap,
                "daily_cap": x.daily_cap,
                # the gap between sends limits a sender too: a 60–180 s SMTP mailbox does ~30 an hour
                "gap_limit": int(window_seconds / max(MIN_GAP_SEC, (x.gap_min_sec + x.gap_max_sec) / 2))}
               for x in pool]
    if campaign.status == "draft":
        start = max(today, campaign.start_at.astimezone(tz).date()) if campaign.start_at else today
        rows = db.execute(eligible_leads(campaign).with_only_columns(Lead.domain, Lead.is_free_mail)).all()
        leads = [(domain, free, 1, start) for domain, free in rows]
    else:
        start = today
        rows = db.execute(select(Lead.domain, Lead.is_free_mail, Enrollment.current_step, Enrollment.next_send_at)
                          .join(Enrollment, Enrollment.lead_id == Lead.id)
                          .where(Enrollment.campaign_id == campaign.id, Enrollment.state == "active")).all()
        leads = [(domain, free, step, max(start, due.astimezone(tz).date()) if due else start)
                 for domain, free, step, due in rows]
    if not leads:
        return {"problem": "No leads left to email."}
    days, unsent = send_plan(leads, start=start, weekdays=campaign.schedule.get("weekdays", []),
                             delays={st.step_no: st.delay_business_days for st in steps if st.step_no > 1},
                             campaign_cap=campaign.daily_cap, senders=senders, domain_max=s.domain_daily_max)
    if not days:
        return {"problem": "Nothing could be sent in the next year with these settings (check days and caps)."}
    weekly = len(days) > 45  # a long plan reads better as weeks
    if weekly:
        weeks: dict[date, dict[int, int]] = {}
        for day, sent in days:
            bucket = weeks.setdefault(day - timedelta(days=day.weekday()), {})
            for step, n in sent.items():
                bucket[step] = bucket.get(step, 0) + n
        bars = list(weeks.items())
    else:
        bars = days
    return {
        "bars": [(when, sent, sum(sent.values())) for when, sent in bars],
        "weekly": weekly,
        "steps": [st.step_no for st in steps],
        "total": sum(sum(sent.values()) for _, sent in days),
        "send_days": len(days),
        "first": days[0][0],
        "last": days[-1][0],
        "unsent": unsent,
        "busiest": max(sum(sent.values()) for _, sent in bars),
    }


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
