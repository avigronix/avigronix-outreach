from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
    func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

ROLES = ("admin", "manager", "sales")


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('admin', 'manager', 'sales')", name="ck_users_role"),)

    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))  # admin / manager / sales
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Bumped on password reset or disable, so old session cookies stop working.
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(TimestampMixin, Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_created_at", "created_at"),)

    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(60), index=True)
    entity: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    ip: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)


class AppSetting(TimestampMixin, Base):
    """Single row (id=1) of settings an admin can change from the Settings page."""

    __tablename__ = "settings"

    company_name: Mapped[str] = mapped_column(String(200), default="Avigronix Technologies")
    postal_address: Mapped[str] = mapped_column(Text, default="")
    cap_day_tz: Mapped[str] = mapped_column(String(64), default="America/New_York")
    bounce_pause_pct: Mapped[float] = mapped_column(Float, default=3.0)
    complaint_pause_pct: Mapped[float] = mapped_column(Float, default=0.1)
    pause_min_sends: Mapped[int] = mapped_column(Integer, default=50)
    domain_daily_max: Mapped[int] = mapped_column(Integer, default=3)
    worker_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_daily_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_backup_note: Mapped[str] = mapped_column(Text, default="")


SOURCE_TYPES = {
    "opt_in": "Opt-in",
    "existing_client": "Existing client",
    "inquiry": "Inquiry",
    "event": "Event",
    "purchased": "Purchased database",
    "manual_research": "Manual research",
}

LEAD_STATUSES = {
    "new": "New",
    "in_sequence": "In sequence",
    "replied": "Replied",
    "interested": "Interested",
    "not_interested": "Not interested",
    "bounced": "Bounced",
    "unsubscribed": "Unsubscribed",
    "complained": "Complained",
    "do_not_contact": "Do not contact",
}


class LeadImport(TimestampMixin, Base):
    """One uploaded file. The worker validates and imports it (status: uploaded → validating →
    validated → importing → done, or failed)."""

    __tablename__ = "lead_imports"

    file_name: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    headers: Mapped[list] = mapped_column(JSONB, default=list)
    preview: Mapped[list] = mapped_column(JSONB, default=list)  # first 20 rows
    column_map: Mapped[dict] = mapped_column(JSONB, default=dict)  # file column index → field
    source_type: Mapped[str | None] = mapped_column(String(30))
    list_name: Mapped[str | None] = mapped_column(String(200))
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    status: Mapped[str] = mapped_column(String(20), default="uploaded")
    error: Mapped[str | None] = mapped_column(Text)
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    # Validation counts (see services/importer.py)
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    imported: Mapped[int] = mapped_column(Integer, default=0)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MxCache(TimestampMixin, Base):
    __tablename__ = "mx_cache"

    domain: Mapped[str] = mapped_column(String(255), unique=True)
    has_mx: Mapped[bool] = mapped_column(Boolean)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Lead(TimestampMixin, Base):
    __tablename__ = "leads"
    __table_args__ = (Index("ix_leads_tags", "tags", postgresql_using="gin"),)

    email: Mapped[str] = mapped_column(String(320), unique=True)  # always lower-case
    domain: Mapped[str] = mapped_column(String(255), index=True)
    first_name: Mapped[str] = mapped_column(String(120), default="")
    last_name: Mapped[str] = mapped_column(String(120), default="")
    company: Mapped[str] = mapped_column(String(255), default="")
    title: Mapped[str] = mapped_column(String(255), default="")
    website: Mapped[str] = mapped_column(String(255), default="")
    city: Mapped[str] = mapped_column(String(120), default="")
    state: Mapped[str] = mapped_column(String(120), default="")
    country: Mapped[str] = mapped_column(String(120), default="")  # ISO-2 code when recognised
    timezone: Mapped[str | None] = mapped_column(String(64))  # None → campaign default at send time
    industry: Mapped[str] = mapped_column(String(120), default="")
    custom: Mapped[dict] = mapped_column(JSONB, default=dict)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    source_type: Mapped[str] = mapped_column(String(30))
    import_id: Mapped[int | None] = mapped_column(ForeignKey("lead_imports.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    is_role: Mapped[bool] = mapped_column(Boolean, default=False)
    is_free_mail: Mapped[bool] = mapped_column(Boolean, default=False)
    soft_bounce_count: Mapped[int] = mapped_column(Integer, default=0)


class Suppression(TimestampMixin, Base):
    """Never email this address (kind=email) or anyone at this domain (kind=domain)."""

    __tablename__ = "suppressions"

    value: Mapped[str] = mapped_column(String(320), unique=True)  # lower-case email or domain
    kind: Mapped[str] = mapped_column(String(10))  # email / domain
    reason: Mapped[str] = mapped_column(String(30))  # unsubscribe / hard_bounce / complaint / manual / erasure
    source: Mapped[str] = mapped_column(String(100), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class Template(TimestampMixin, Base):
    __tablename__ = "templates"

    name: Mapped[str] = mapped_column(String(200))
    subject_variants: Mapped[list[str]] = mapped_column(JSONB, default=list)  # 1–3 subjects, rotated A/B/C
    body_text: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[str] = mapped_column(Text, default="")  # optional; empty = plain text only
    test_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class Sender(TimestampMixin, Base):
    """A sending identity: an SES verified address or an SMTP mailbox."""

    __tablename__ = "senders"
    __table_args__ = (CheckConstraint("type IN ('ses', 'smtp')", name="ck_senders_type"),)

    type: Mapped[str] = mapped_column(String(10))
    from_name: Mapped[str] = mapped_column(String(120))
    from_email: Mapped[str] = mapped_column(String(320), unique=True)
    reply_to: Mapped[str] = mapped_column(String(320))
    smtp_host: Mapped[str] = mapped_column(String(255), default="")
    smtp_port: Mapped[int] = mapped_column(Integer, default=465)
    smtp_user: Mapped[str] = mapped_column(String(320), default="")
    smtp_password_enc: Mapped[str] = mapped_column(Text, default="")  # Fernet
    imap_host: Mapped[str] = mapped_column(String(255), default="")  # for bounce (DSN) reading, Phase 6
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    dkim_selector: Mapped[str] = mapped_column(String(100), default="")  # SMTP senders, for the DNS check
    daily_cap: Mapped[int] = mapped_column(Integer)
    gap_min_sec: Mapped[int] = mapped_column(Integer)
    gap_max_sec: Mapped[int] = mapped_column(Integer)
    warmup_day: Mapped[int] = mapped_column(Integer, default=1)
    warmup_override_cap: Mapped[int | None] = mapped_column(Integer)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active / paused / disabled
    pause_reason: Mapped[str] = mapped_column(String(255), default="")
    dns_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dns_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    dns_result: Mapped[dict] = mapped_column(JSONB, default=dict)
    health_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # auto-pause counts sends after this

    @property
    def domain(self) -> str:
        return self.from_email.rsplit("@", 1)[1]


CAMPAIGN_STATUSES = ("draft", "scheduled", "running", "paused", "completed", "stopped")
DEFAULT_SCHEDULE = {
    "windows": [["09:00", "11:30"], ["13:30", "16:30"]],
    "weekdays": [0, 1, 2, 3, 4],  # Mon–Fri
    "default_tz": "America/New_York",
}


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"
    __table_args__ = (CheckConstraint("provider IN ('ses', 'smtp')", name="ck_campaigns_provider"),)

    name: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(10))
    segment: Mapped[dict] = mapped_column(JSONB, default=dict)  # {"tags": [...], "countries": [...]}
    schedule: Mapped[dict] = mapped_column(JSONB, default=dict)  # see DEFAULT_SCHEDULE
    daily_cap: Mapped[int] = mapped_column(Integer, default=400)
    stop_company_on_reply: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_canada: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    pause_reason: Mapped[str] = mapped_column(String(255), default="")
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # auto-pause counts sends after this
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class CampaignSender(TimestampMixin, Base):
    __tablename__ = "campaign_senders"
    __table_args__ = (UniqueConstraint("campaign_id", "sender_id"),)

    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    sender_id: Mapped[int] = mapped_column(ForeignKey("senders.id"))


class SequenceStep(TimestampMixin, Base):
    __tablename__ = "sequence_steps"
    __table_args__ = (UniqueConstraint("campaign_id", "step_no"),)

    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    step_no: Mapped[int] = mapped_column(Integer)  # 1..5
    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id"))
    delay_business_days: Mapped[int] = mapped_column(Integer, default=0)  # after the previous step
    same_thread: Mapped[bool] = mapped_column(Boolean, default=True)


class Enrollment(TimestampMixin, Base):
    """One lead in one campaign. current_step = the next step to send."""

    __tablename__ = "enrollments"
    __table_args__ = (
        UniqueConstraint("campaign_id", "lead_id"),
        # FR-4.7: a lead is active in at most one campaign.
        Index("uq_enrollments_one_active_per_lead", "lead_id", unique=True,
              postgresql_where=text("state = 'active'")),
        Index("ix_enrollments_due", "state", "next_send_at"),
    )

    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"))
    sender_id: Mapped[int | None] = mapped_column(ForeignKey("senders.id"))  # fixed after step 1 (threading)
    current_step: Mapped[int] = mapped_column(Integer, default=1)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # NULL = waiting on a retry/check
    state: Mapped[str] = mapped_column(String(20), default="active")  # active / completed / stopped
    stop_reason: Mapped[str] = mapped_column(String(40), default="")
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(TimestampMixin, Base):
    """One email for one step. The row is committed BEFORE the provider is called (core rule 2)."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("enrollment_id", "step_no", name="uq_messages_enrollment_step"),
        Index("ix_messages_sender_created", "sender_id", "created_at"),
        Index("ix_messages_campaign_created", "campaign_id", "created_at"),
        Index("ix_messages_domain_created", "to_domain", "created_at"),
    )

    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id", ondelete="CASCADE"))
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("senders.id"))
    step_no: Mapped[int] = mapped_column(Integer)
    variant: Mapped[str] = mapped_column(String(1), default="A")
    to_email: Mapped[str] = mapped_column(String(320))
    to_domain: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str] = mapped_column(Text, default="")
    message_id_header: Mapped[str] = mapped_column(String(255), unique=True)
    in_reply_to: Mapped[str] = mapped_column(String(255), default="")
    references: Mapped[str] = mapped_column(Text, default="")
    provider_message_id: Mapped[str] = mapped_column(String(255), default="", index=True)
    status: Mapped[str] = mapped_column(String(20), default="sending", index=True)  # sending/sent/retry/failed/unknown
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str] = mapped_column(Text, default="")


EVENT_TYPES = ("delivery", "bounce_hard", "bounce_soft", "complaint", "reject", "reply", "auto_reply", "unsubscribe")


class Event(TimestampMixin, Base):
    """Something that happened to a sent email (SES/SNS, DSN bounce, reply...)."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_type_occurred", "type", "occurred_at"),)

    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    sender_id: Mapped[int | None] = mapped_column(ForeignKey("senders.id"))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(20))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_id: Mapped[str | None] = mapped_column(String(255), unique=True)  # e.g. SNS MessageId: redeliveries are ignored
    detail: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)  # cleared after 90 days


REPLY_OUTCOMES = {
    "interested": "Interested",
    "meeting_booked": "Meeting booked",
    "not_interested": "Not interested",
    "wrong_person": "Wrong person",
    "unsubscribe_request": "Unsubscribe request",
}


class Reply(TimestampMixin, Base):
    """An email in a lead's conversation after our sequence: their answers (direction "in", read by IMAP)
    and our replies sent from the dashboard (direction "out")."""

    __tablename__ = "replies"
    __table_args__ = (Index("ix_replies_inbox", "classification", "outcome", "received_at"),)

    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))  # what they replied to
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    mailbox: Mapped[str] = mapped_column(String(320))
    message_id_header: Mapped[str] = mapped_column(String(500), unique=True)
    from_email: Mapped[str] = mapped_column(String(320))
    from_name: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")  # quoted history removed
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    classification: Mapped[str] = mapped_column(String(30))  # reply / auto_reply / unsubscribe_request / sent
    assigned_to: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    outcome: Mapped[str | None] = mapped_column(String(30))  # see REPLY_OUTCOMES
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    direction: Mapped[str] = mapped_column(String(3), default="in", server_default="in")
    sent_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))  # direction "out"
    draft: Mapped[str] = mapped_column(Text, default="", server_default="")  # unsent answer to this reply


class ImapCursor(TimestampMixin, Base):
    """How far each mailbox has been read (by IMAP UID), so mail is processed once."""

    __tablename__ = "imap_cursors"

    mailbox: Mapped[str] = mapped_column(String(320), unique=True)
    uidvalidity: Mapped[int] = mapped_column(BigInteger, default=0)
    last_uid: Mapped[int] = mapped_column(BigInteger, default=0)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(Text, default="")


def get_app_settings(db: Session) -> AppSetting:
    """The single settings row (created by the first migration)."""
    return db.get(AppSetting, 1)
