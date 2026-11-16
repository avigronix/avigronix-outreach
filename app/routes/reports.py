import csv
import io
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import get_db
from app.models import Campaign, Message, Sender, User, get_app_settings
from app.services import reports
from app.services.audit import audit
from app.services.warmup import effective_daily_cap
from app.ui import render

router = APIRouter(prefix="/reports")
managers = require_role("admin", "manager")

STAT_COLUMNS = [("sends", "Sent"), ("delivered", "Delivered"), ("bounces", "Bounced"), ("complaints", "Complaints"),
                ("unsubscribed", "Unsubscribed"), ("replies", "Replied"), ("positive", "Positive")]
SENDER_DAYS = 14


def csv_response(filename: str, header: list[str], rows: list[list]) -> Response:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return Response("﻿" + out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def stat_cells(stats: dict) -> list:
    cells = []
    for key, _ in STAT_COLUMNS:
        cells.append(stats[key])
        if key != "sends":
            cells.append(round(stats[key + "_pct"], 2))
    return cells


def stat_header(prefix: list[str]) -> list[str]:
    header = list(prefix)
    for key, label in STAT_COLUMNS:
        header.append(label)
        if key != "sends":
            header.append(label + " %")
    return header


def sender_days(db: Session, sender: Sender, cap_tz: str, now: datetime) -> list[tuple]:
    """(day, sent, bounces, complaints) for the last SENDER_DAYS business days."""
    tz = ZoneInfo(cap_tz)
    rows = []
    for day, sent in reports.daily_sends(db, now, cap_tz, SENDER_DAYS, Message.sender_id == sender.id):
        start = datetime.combine(day, time(0), tzinfo=tz)
        st = reports.message_stats(db, Message.sender_id == sender.id, Message.created_at >= start,
                                   Message.created_at < start + timedelta(days=1))
        rows.append((day, sent, st["bounces"], st["complaints"]))
    return rows


@router.get("")
def reports_page(request: Request, campaign_id: int | None = None, db: Session = Depends(get_db),
                 user: User = Depends(managers)):
    s = get_app_settings(db)
    now = datetime.now(timezone.utc)
    rows = reports.campaign_rows(db)
    with_sends = [r["c"] for r in rows if r["stats"]["sends"]] or [r["c"] for r in rows]
    selected = db.get(Campaign, campaign_id) if campaign_id else (with_sends[0] if with_sends else None)
    senders = db.scalars(select(Sender).where(Sender.status != "disabled").order_by(Sender.from_email)).all()
    return render(
        request, "reports.html", rows=rows, selected=selected,
        report=reports.campaign_report(db, selected) if selected else None,
        senders=[(x, effective_daily_cap(x), sender_days(db, x, s.cap_day_tz, now)) for x in senders],
        columns=STAT_COLUMNS, bounce_level=reports.bounce_level, complaint_level=reports.complaint_level,
    )


@router.get("/campaigns.csv")
def campaigns_csv(request: Request, db: Session = Depends(get_db), user: User = Depends(managers)):
    rows = [[r["c"].name, r["c"].provider, r["c"].status, r["leads"], *stat_cells(r["stats"])]
            for r in reports.campaign_rows(db)]
    audit(db, request, user, "report_exported", report="campaigns")
    db.commit()
    return csv_response("campaigns.csv", stat_header(["Campaign", "Provider", "Status", "Leads"]), rows)


@router.get("/campaign/{campaign_id}.csv")
def campaign_csv(request: Request, campaign_id: int, db: Session = Depends(get_db), user: User = Depends(managers)):
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    report = reports.campaign_report(db, campaign)
    rows = [["Total", "", *stat_cells(report["total"])]]
    rows += [[f"Step {st.step_no}", "", *stat_cells(stats)] for st, stats in report["steps"]]
    rows += [["Subject variant", v, *stat_cells(stats)] for v, stats in report["variants"]]
    audit(db, request, user, "report_exported", "campaign", campaign.id)
    db.commit()
    return csv_response(f"campaign-{campaign.id}.csv", stat_header(["Row", "Variant"]), rows)


@router.get("/senders.csv")
def senders_csv(request: Request, db: Session = Depends(get_db), user: User = Depends(managers)):
    s = get_app_settings(db)
    now = datetime.now(timezone.utc)
    rows = []
    for sender in db.scalars(select(Sender).order_by(Sender.from_email)):
        for day, sent, bounces, complaints in sender_days(db, sender, s.cap_day_tz, now):
            rows.append([sender.from_email, sender.type, day.isoformat(), sent, bounces, complaints,
                         sender.warmup_day, effective_daily_cap(sender)])
    audit(db, request, user, "report_exported", report="senders")
    db.commit()
    return csv_response("sender-health.csv", ["Sender", "Type", "Day", "Sent", "Hard bounces", "Complaints",
                                              "Warm-up day (now)", "Daily cap (now)"], rows)
