"""Nightly database backup (NFR-7, decision D2): pg_dump → BACKUP_DIR (kept 7 days) → S3 (30-day lifecycle on the bucket).

Restore: pg_restore --clean --no-owner -d <database> <file>.dump  (see README).
"""

import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import boto3
from sqlalchemy.engine import make_url

from app.config import settings

log = logging.getLogger("backup")

KEEP_LOCAL_DAYS = 7


def dump_database(path: Path) -> None:
    url = make_url(settings.database_url)
    command = ["pg_dump", "--format=custom", "--no-owner", "-h", url.host or "localhost", "-p", str(url.port or 5432),
               "-U", url.username or "postgres", "-d", url.database or "postgres", "-f", str(path)]
    env = {**os.environ, "PGPASSWORD": url.password or ""}
    try:
        subprocess.run(command, check=True, env=env, capture_output=True, text=True, timeout=1800)
    except FileNotFoundError:
        raise RuntimeError("pg_dump is not installed (apt install postgresql-16 or postgresql-client-16)")
    except subprocess.CalledProcessError as exc:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump failed: {exc.stderr.strip()[:500]}")


def upload_to_s3(path: Path) -> str:
    key = settings.backup_s3_prefix + path.name
    client = boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.backup_aws_access_key_id or settings.aws_access_key_id,
        aws_secret_access_key=settings.backup_aws_secret_access_key or settings.aws_secret_access_key,
    )
    client.upload_file(str(path), settings.backup_s3_bucket, key)
    return f"s3://{settings.backup_s3_bucket}/{key}"


def prune_local(folder: Path, now: float) -> int:
    removed = 0
    for old in folder.glob("outreach-*.dump"):
        if now - old.stat().st_mtime > KEEP_LOCAL_DAYS * 86400:
            old.unlink()
            removed += 1
    return removed


def run_backup(now: datetime) -> str:
    """Make one backup. Returns a short note; raises RuntimeError with the reason on failure."""
    folder = Path(settings.backup_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"outreach-{now:%Y%m%d-%H%M}.dump"
    dump_database(path)
    size_mb = path.stat().st_size / 1_048_576
    note = f"{path.name} ({size_mb:.1f} MB)"
    if settings.backup_s3_bucket:
        try:
            note += f" → {upload_to_s3(path)}"
        except Exception as exc:
            raise RuntimeError(f"{note} saved locally, but the S3 upload failed: {exc}")
    else:
        note += " — local only (BACKUP_S3_BUCKET not set)"
    removed = prune_local(folder, time.time())
    if removed:
        note += f"; removed {removed} local backup(s) older than {KEEP_LOCAL_DAYS} days"
    return note


def local_backups() -> list[tuple[str, float, float]]:
    """(name, size MB, modified timestamp) for the Settings page, newest first."""
    folder = Path(settings.backup_dir)
    if not folder.exists():
        return []
    files = sorted(folder.glob("outreach-*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [(p.name, p.stat().st_size / 1_048_576, p.stat().st_mtime) for p in files]
