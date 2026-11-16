"""`python -m app.seed`              — create the first admin from SEED_ADMIN_* in .env (safe to run again).
`python -m app.seed --demo`       — also add demo data so every screen has something to show.
`python -m app.seed --remove-demo` — delete all demo data again.

Demo data is for a test install. Its leads and senders use .example domains (which can never receive mail),
and its campaigns' next sends are 60 days away, so the worker doesn't try to send anything.
"""

import random
import secrets
import sys
import uuid
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.auth import hash_password, password_problem
from app.config import settings
from app.db import SessionLocal
from app.models import (
    DEFAULT_SCHEDULE, AuditLog, Campaign, CampaignSender, Enrollment, Event, Lead, Message, Reply, Sender, SequenceStep,
    Suppression, Template, User,
)
from app.services.audit import audit
from app.services.crypto import encrypt_secret
from app.services.scheduler import is_business_day

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
DEMO_DOMAIN = ".example"
DEMO_MANAGER = "avinash.demo@avigronix.example"


def seed_admin(db) -> None:
    email = settings.seed_admin_email.strip().lower()
    password = settings.seed_admin_password
    if not email or not password:
        sys.exit("Set SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD in .env first.")
    problem = password_problem(password)
    if problem:
        sys.exit(f"SEED_ADMIN_PASSWORD: {problem}")

    if db.scalar(select(User.id).where(func.lower(User.email) == email)):
        print(f"Admin {email} already exists, nothing to do.")
        return
    admin = User(name=settings.seed_admin_name, email=email, role="admin", password_hash=hash_password(password))
    db.add(admin)
    db.flush()
    audit(db, None, None, "user_created", "user", admin.id, email=email, role="admin", via="seed")
    db.commit()
    print(f"Created admin {email}. You can log in now.")


# ---------- demo data ----------

NAMED_LEADS = [
    # first, last, company, title, domain, city, state, country, tz, tag
    ("Jason", "Miller", "BrightSmile Dental", "Owner, DDS", "brightsmiledental", "Austin", "TX", "US", "America/Chicago", "us-dental"),
    ("Mark", "Turner", "Turner Orthodontics", "Practice Manager", "turnerortho", "Denver", "CO", "US", "America/Denver", "us-dental"),
    ("Emily", "Davis", "Lakeside Family Dentistry", "Office Manager", "lakesidedental", "Chicago", "IL", "US", "America/Chicago", "us-dental"),
    ("Sarah", "Nguyen", "Pacific Smiles", "DDS", "pacificsmiles", "San Diego", "CA", "US", "America/Los_Angeles", "us-dental"),
    ("Rachel", "Kim", "Kim Realty Group", "Broker", "kimrealty", "Phoenix", "AZ", "US", "America/Phoenix", "us-real-estate"),
    ("Carlos", "Rivera", "Rivera Homes", "Owner", "riverahomes", "Miami", "FL", "US", "America/New_York", "us-real-estate"),
    ("Tom", "Becker", "Becker & Co Realty", "Managing Broker", "beckerrealty", "Columbus", "OH", "US", "America/New_York", "us-real-estate"),
    ("Priya", "Shah", "Northlane Labs", "CTO", "northlane", "London", "", "GB", "Europe/London", "uk-startups"),
    ("Oliver", "Grant", "Grantwell Studio", "CEO", "grantwell", "Manchester", "", "GB", "Europe/London", "uk-startups"),
    ("Aisha", "Khan", "Medlytics", "Founder", "medlytics", "Boston", "MA", "US", "America/New_York", "healthcare-saas"),
]
FIRST = ["James", "Linda", "Robert", "Patricia", "Michael", "Jennifer", "David", "Susan", "Daniel", "Karen", "Paul",
         "Nancy", "Steven", "Lisa", "Kevin", "Betty", "Brian", "Helen", "George", "Sandra", "Chris", "Amy", "Ryan"]
LAST = ["Smith", "Johnson", "Brown", "Garcia", "Wilson", "Moore", "Taylor", "Clark", "Lewis", "Walker", "Hall",
        "Young", "King", "Wright", "Scott", "Green", "Baker", "Adams", "Nelson", "Hill", "Campbell", "Mitchell"]
CITIES = [("Austin", "TX", "America/Chicago"), ("Dallas", "TX", "America/Chicago"), ("Denver", "CO", "America/Denver"),
          ("Seattle", "WA", "America/Los_Angeles"), ("Atlanta", "GA", "America/New_York"),
          ("Columbus", "OH", "America/New_York"), ("Phoenix", "AZ", "America/Phoenix"),
          ("Nashville", "TN", "America/Chicago"), ("Charlotte", "NC", "America/New_York")]
KINDS = {"us-dental": ("Dental", "Dental Care", "DDS"), "us-real-estate": ("Realty", "Real Estate", "Broker"),
         "uk-startups": ("Labs", "Software", "Founder"), "healthcare-saas": ("Health", "Healthcare", "CEO")}

TEMPLATES = {
    "dental-intro": (["Quick idea for {{company}}'s website", "{{first_name|there}}, new patients from your site?"],
                     "Hi {{first_name|there}},\n\nI was looking at {{company}}'s website and noticed patients can't book "
                     "online. We rebuild booking flows for dental practices in {{state|your state}}, and the last two "
                     "practices we worked with saw online appointment requests go up within a month.\n\nWould a "
                     "15-minute call next week be useful? I can show you what we'd change.\n\n{{sender_name}}"),
    "dental-fu1": (["x"], "Hi {{first_name|there}}, following up on my note last week. Happy to send two examples "
                          "first if that's easier.\n\n{{sender_name}}"),
    "dental-fu2": (["x"], "Hi {{first_name|there}}, here is a short case study: https://avigronix.com/work. "
                          "Worth a quick look?\n\n{{sender_name}}"),
    "crm-intro": (["A simple CRM for {{company}}?"],
                  "Hi {{first_name|there}},\n\nMost agencies we talk to track leads in spreadsheets and lose follow-ups. "
                  "We build small, simple CRMs for real estate teams. Would it help to see one?\n\n{{sender_name}}"),
    "mvp-intro": (["MVP for {{company}}"],
                  "Hi {{first_name|there}},\n\nWe help early-stage teams ship their first version in 8 weeks. Open to a "
                  "short call?\n\n{{sender_name}}"),
}

REPLIES = [
    # lead first name, reply text, classification, outcome, hours ago
    ("Jason", "Hi Vikas, sounds interesting. Our site is pretty dated and we get almost no bookings through it. "
              "Can you share two recent projects and a rough timeline? Happy to talk next week.", "reply", "interested", 2),
    ("Rachel", "Roughly what would a CRM like this cost us? We're a team of 6.", "reply", None, 5),
    ("Mark", "Not my area, please reach out to our office manager.", "reply", None, 26),
    ("Priya", "Please remove me from your list.", "unsubscribe_request", None, 28),
    ("Emily", "I'm out of the office until Monday with limited access to email.", "auto_reply", None, 40),
]


def demo_present(db) -> bool:
    return db.scalar(select(Lead.id).where(Lead.tags.any("demo")).limit(1)) is not None


def business_days_back(n: int) -> list:
    days, d = [], datetime.now(NY).date()
    while len(days) < n:
        if is_business_day(d):
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def at(day, hour: int, minute: int = 0):
    return datetime.combine(day, time(hour, minute), tzinfo=NY).astimezone(UTC)


def make_leads(db, rng) -> dict[str, list[Lead]]:
    leads: dict[str, list[Lead]] = {tag: [] for tag in KINDS}
    for first, last, company, title, domain, city, state, country, tz, tag in NAMED_LEADS:
        leads[tag].append(Lead(email=f"{first.lower()}@{domain}{DEMO_DOMAIN}", domain=domain + DEMO_DOMAIN,
                               first_name=first, last_name=last, company=company, title=title,
                               website=f"{domain}{DEMO_DOMAIN}", city=city, state=state, country=country, timezone=tz,
                               tags=["demo", tag], source_type="manual_research"))
    sizes = {"us-dental": 220, "us-real-estate": 120, "uk-startups": 70, "healthcare-saas": 40}
    for tag, size in sizes.items():
        word, industry, title = KINDS[tag]
        while len(leads[tag]) < size:
            i = len(leads[tag])
            first, last = rng.choice(FIRST), rng.choice(LAST)
            company = f"{last} {word}" if i % 2 else f"{rng.choice(CITIES)[0]} {word} {i}"
            domain = f"{company.lower().replace(' ', '')}{i}{DEMO_DOMAIN}"
            city, state, tz = rng.choice(CITIES)
            country = "GB" if tag == "uk-startups" else "US"
            leads[tag].append(Lead(
                email=f"{first.lower()}@{domain}", domain=domain, first_name=first, last_name=last, company=company,
                title=title, website=domain, city="London" if country == "GB" else city,
                state="" if country == "GB" else state, country=country,
                timezone="Europe/London" if country == "GB" else tz, industry=industry, tags=["demo", tag],
                source_type="purchased" if tag == "uk-startups" else "opt_in", custom={"rating": f"{rng.uniform(3.8, 5):.1f}"}))
    for group in leads.values():
        db.add_all(group)
    db.flush()
    return leads


def make_campaign(db, name, provider, tag, sender, steps, status, cap=400, start_at=None, pause_reason=""):
    c = Campaign(name=name, provider=provider, segment={"tags": [tag], "countries": [], "demo": True},
                 schedule=DEFAULT_SCHEDULE, daily_cap=cap, status=status, start_at=start_at,
                 started_at=datetime.now(UTC) - timedelta(days=14) if status != "scheduled" else None,
                 pause_reason=pause_reason, stop_company_on_reply=True)
    db.add(c)
    db.flush()
    db.add(CampaignSender(campaign_id=c.id, sender_id=sender.id))
    for n, (template, delay) in enumerate(steps, start=1):
        db.add(SequenceStep(campaign_id=c.id, step_no=n, template_id=template.id, delay_business_days=delay,
                            same_thread=True))
    db.flush()
    return c


def send_history(db, rng, campaign, sender, leads, templates, per_day, delays, bounce_rate, days):
    """Fake sent messages (and their delivery/bounce events) across the last business days."""
    later = datetime.now(UTC) + timedelta(days=60)
    queue = list(leads)
    sent_by_lead: dict[int, list[Message]] = {}
    for day_index, day in enumerate(days):
        todays = queue[:per_day[day_index]]
        queue = queue[per_day[day_index]:]
        for i, lead in enumerate(todays):
            sent_by_lead[lead.id] = [(day_index, i)]
    enrollments = {}
    for lead in leads:
        enr = Enrollment(campaign_id=campaign.id, lead_id=lead.id, sender_id=sender.id, state="active",
                         current_step=1, next_send_at=later)
        db.add(enr)
        enrollments[lead.id] = enr
    db.flush()

    subject_variants = templates[0].subject_variants
    for lead in leads:
        if lead.id not in sent_by_lead:
            enrollments[lead.id].sender_id = None
            continue
        enr, (day_index, slot) = enrollments[lead.id], sent_by_lead[lead.id][0]
        previous: list[Message] = []
        step_day = day_index
        for step_no, template in enumerate(templates, start=1):
            if step_no > 1:
                step_day += delays[step_no - 1]
                if step_day >= len(days):
                    break
            when = min(at(days[step_day], 9 + slot % 7, (slot * 7) % 60), datetime.now(UTC) - timedelta(minutes=slot + 1))
            variant_index = enr.id % len(subject_variants)
            subject = subject_variants[variant_index] if step_no == 1 else "Re: " + previous[0].subject
            subject = subject.replace("{{company}}", lead.company)
            subject = subject.replace("{{first_name|there}}", lead.first_name)
            m = Message(enrollment_id=enr.id, campaign_id=campaign.id, lead_id=lead.id, sender_id=sender.id,
                        step_no=step_no, variant="AB"[variant_index] if step_no == 1 else previous[0].variant,
                        to_email=lead.email, to_domain=lead.domain, subject=subject,
                        body_text=template.body_text.replace("{{first_name|there}}", lead.first_name)
                        .replace("{{company}}", lead.company).replace("{{sender_name}}", sender.from_name.split()[0])
                        .replace("{{state|your state}}", lead.state or "your area"),
                        message_id_header=f"<demo-{uuid.uuid4().hex}@{sender.domain}>",
                        in_reply_to=previous[-1].message_id_header if previous else "",
                        provider_message_id=f"demo-{uuid.uuid4().hex}", status="sent", sent_at=when, created_at=when)
            db.add(m)
            db.flush()
            previous.append(m)
            if rng.random() < bounce_rate and step_no == 1:
                db.add(Event(message_id=m.id, lead_id=lead.id, sender_id=sender.id, campaign_id=campaign.id,
                             type="bounce_hard", occurred_at=when + timedelta(minutes=1),
                             detail="Permanent/General: 550 5.1.1 user unknown"))
                db.add(Suppression(value=lead.email, kind="email", reason="hard_bounce", source="demo"))
                lead.status = "bounced"
                enr.state, enr.stop_reason, enr.next_send_at = "stopped", "bounced", None
                break
            if sender.type == "ses":
                db.add(Event(message_id=m.id, lead_id=lead.id, sender_id=sender.id, campaign_id=campaign.id,
                             type="delivery", occurred_at=when + timedelta(seconds=30)))
        if enr.state == "active":
            lead.status = "in_sequence"
            enr.current_step = len(previous) + 1
            if enr.current_step > len(templates):
                enr.state, enr.next_send_at = "completed", None
    db.flush()
    return enrollments


def seed_demo(db) -> None:
    if demo_present(db):
        print("Demo data is already there (python -m app.seed --remove-demo to start over).")
        return
    real = db.scalar(select(func.count()).select_from(Campaign).where(~Campaign.segment.has_key("demo")))  # noqa: W601
    if real:
        sys.exit("This database already has real campaigns. Demo data is only for a test install.")
    rng = random.Random(42)
    now = datetime.now(UTC)
    admin = db.scalar(select(User).where(User.role == "admin").order_by(User.id))

    manager = db.scalar(select(User).where(User.email == DEMO_MANAGER))
    manager_password = None
    if manager is None:
        manager_password = secrets.token_urlsafe(9)
        manager = User(name="Avinash", email=DEMO_MANAGER, role="manager", password_hash=hash_password(manager_password))
        db.add(manager)

    ses = Sender(type="ses", from_name="Vikas from Avigronix", from_email=f"vikas@mail.avigronix{DEMO_DOMAIN}",
                 reply_to=f"outreach-replies@avigronix{DEMO_DOMAIN}", daily_cap=2000, gap_min_sec=2, gap_max_sec=6,
                 warmup_day=16, status="active", dns_ok=True, dns_checked_at=now,
                 dns_result={"SES identity": [True, "demo"], "DKIM (Easy DKIM)": [True, "SUCCESS"],
                             "Custom MAIL FROM (SPF)": [True, "SUCCESS"], "DMARC": [True, "v=DMARC1; p=none"]})
    smtp = Sender(type="smtp", from_name="Avinash from Avigronix", from_email=f"avinash@avigronix-in{DEMO_DOMAIN}",
                  reply_to=f"avinash@avigronix-in{DEMO_DOMAIN}", smtp_host="smtp.invalid", smtp_port=465,
                  smtp_user="demo", smtp_password_enc=encrypt_secret("demo-not-a-real-password"),
                  daily_cap=50, gap_min_sec=60, gap_max_sec=180, warmup_day=9, status="paused", dns_ok=True,
                  pause_reason="Auto-paused: bounce rate 3.3% over the last 120 sends (limit 3%)")
    db.add_all([ses, smtp])
    templates = {}
    for name, (subjects, body) in TEMPLATES.items():
        templates[name] = Template(name=name, subject_variants=subjects, body_text=body, test_sent_at=now,
                                   created_by=admin.id if admin else None)
        db.add(templates[name])
    db.flush()

    leads = make_leads(db, rng)
    days = business_days_back(10)
    ramp = [12, 16, 20, 24, 28, 32, 36, 40, 44, 30]

    dental = make_campaign(db, "US Dental Clinics — Website Revamp", "ses", "us-dental", ses,
                           [(templates["dental-intro"], 0), (templates["dental-fu1"], 3), (templates["dental-fu2"], 5)],
                           "running")
    send_history(db, rng, dental, ses, leads["us-dental"],
                 [templates["dental-intro"], templates["dental-fu1"], templates["dental-fu2"]], ramp, [0, 3, 5], 0.013, days)
    realty = make_campaign(db, "US Real Estate — CRM Setup", "ses", "us-real-estate", ses,
                           [(templates["crm-intro"], 0), (templates["dental-fu1"], 4)], "running")
    send_history(db, rng, realty, ses, leads["us-real-estate"], [templates["crm-intro"], templates["dental-fu1"]],
                 [6, 8, 8, 10, 10, 12, 12, 14, 14, 10], [0, 4], 0.016, days)
    uk = make_campaign(db, "UK Startups — MVP Development", "smtp", "uk-startups", smtp,
                       [(templates["mvp-intro"], 0), (templates["dental-fu1"], 3)], "paused",
                       pause_reason="Auto-paused: bounce rate 3.3% over the last 120 sends (limit 3%)")
    send_history(db, rng, uk, smtp, leads["uk-startups"], [templates["mvp-intro"], templates["dental-fu1"]],
                 [0, 0, 6, 7, 8, 9, 10, 10, 10, 0], [0, 3], 0.06, days)
    health = make_campaign(db, "Healthcare SaaS — AI Chatbot", "ses", "healthcare-saas", ses,
                           [(templates["crm-intro"], 0)], "scheduled", start_at=now + timedelta(days=5))
    for lead in leads["healthcare-saas"]:
        db.add(Enrollment(campaign_id=health.id, lead_id=lead.id, state="active", next_send_at=now + timedelta(days=60)))

    by_first = {l.first_name: l for group in leads.values() for l in group[:3]}
    for first, text, classification, outcome, hours in REPLIES:
        lead = by_first.get(first) or next(l for g in leads.values() for l in g if l.first_name == first)
        message = db.scalar(select(Message).where(Message.lead_id == lead.id).order_by(Message.step_no.desc()).limit(1))
        if message is None or lead.status == "bounced":
            continue
        received = now - timedelta(hours=hours)
        db.add(Reply(message_id=message.id, lead_id=lead.id, mailbox=ses.reply_to,
                     message_id_header=f"<demo-reply-{uuid.uuid4().hex}@{lead.domain}>", from_email=lead.email,
                     from_name=f"{lead.first_name} {lead.last_name}", subject="Re: " + message.subject.removeprefix("Re: "),
                     body_text=text, received_at=received, classification=classification, outcome=outcome,
                     assigned_to=manager.id if outcome else None, handled_at=received if outcome else None))
        db.add(Event(message_id=message.id, lead_id=lead.id, sender_id=message.sender_id,
                     campaign_id=message.campaign_id, type="auto_reply" if classification == "auto_reply" else "reply",
                     occurred_at=received, detail=text[:200]))
        if classification != "auto_reply":
            lead.status = "interested" if outcome == "interested" else "replied"
            enr = db.scalar(select(Enrollment).where(Enrollment.lead_id == lead.id, Enrollment.state == "active"))
            if enr:
                enr.state, enr.stop_reason, enr.next_send_at = "stopped", "replied", None
    # a few more replies spread over the week, already handled
    for lead in rng.sample([l for l in leads["us-dental"][10:] if l.status == "in_sequence"], 12):
        message = db.scalar(select(Message).where(Message.lead_id == lead.id).limit(1))
        received = now - timedelta(hours=rng.randint(30, 160))
        outcome = rng.choice(["interested", "interested", "not_interested", "meeting_booked", "wrong_person"])
        db.add(Reply(message_id=message.id, lead_id=lead.id, mailbox=ses.reply_to,
                     message_id_header=f"<demo-reply-{uuid.uuid4().hex}@{lead.domain}>", from_email=lead.email,
                     from_name=lead.first_name, subject="Re: " + message.subject, body_text="Thanks, let's talk.",
                     received_at=received, classification="reply", outcome=outcome, assigned_to=manager.id,
                     handled_at=received))
        db.add(Event(message_id=message.id, lead_id=lead.id, sender_id=message.sender_id,
                     campaign_id=message.campaign_id, type="reply", occurred_at=received))
        lead.status = "interested" if outcome in ("interested", "meeting_booked") else "not_interested"
        db.execute(update(Enrollment).where(Enrollment.lead_id == lead.id, Enrollment.state == "active")
                   .values(state="stopped", stop_reason="replied", next_send_at=None))

    db.execute(insert(Suppression).values(value=f"competitor{DEMO_DOMAIN}", kind="domain", reason="manual",
                                          source="demo").on_conflict_do_nothing())
    db.commit()
    total = db.scalar(select(func.count()).select_from(Message))
    print(f"Demo data added: {sum(len(g) for g in leads.values())} leads, 4 campaigns, {total} sent emails, replies.")
    if manager_password:
        print(f"Demo manager login: {DEMO_MANAGER} / {manager_password}")
    print("Nothing will be sent: demo addresses use .example domains and the next sends are 60 days away.")
    print("Remove it all with: python -m app.seed --remove-demo")


def remove_demo(db) -> None:
    demo_campaigns = select(Campaign.id).where(Campaign.segment.has_key("demo"))  # noqa: W601
    db.execute(delete(Campaign).where(Campaign.id.in_(demo_campaigns)))  # steps, enrollments, messages, events cascade
    db.execute(delete(Lead).where(Lead.tags.any("demo")))
    db.execute(delete(Suppression).where(Suppression.value.like(f"%{DEMO_DOMAIN}")))
    demo_senders = select(Sender.id).where(Sender.from_email.like(f"%{DEMO_DOMAIN}"))
    db.execute(delete(Event).where(Event.sender_id.in_(demo_senders)))
    db.execute(delete(Sender).where(Sender.id.in_(demo_senders)))
    used = select(SequenceStep.template_id)
    db.execute(delete(Template).where(Template.name.in_(TEMPLATES), ~Template.id.in_(used)))
    manager = select(User.id).where(User.email == DEMO_MANAGER)
    db.execute(update(AuditLog).where(AuditLog.user_id.in_(manager)).values(user_id=None))
    db.execute(update(Lead).where(Lead.owner_id.in_(manager)).values(owner_id=None))
    db.execute(update(Reply).where(Reply.assigned_to.in_(manager)).values(assigned_to=None))
    db.execute(update(Reply).where(Reply.sent_by.in_(manager)).values(sent_by=None))
    db.execute(delete(User).where(User.email == DEMO_MANAGER))
    db.commit()
    print("Demo data removed.")


def main() -> None:
    with SessionLocal() as db:
        if "--remove-demo" in sys.argv[1:]:
            remove_demo(db)
            return
        seed_admin(db)
        if "--demo" in sys.argv[1:]:
            seed_demo(db)


if __name__ == "__main__":
    main()
