#!/usr/bin/env bash
# On-demand database backup: pg_dump -> /opt/outreach/data/backups (kept 7 days) -> s3://$BACKUP_S3_BUCKET/$BACKUP_S3_PREFIX
#
# It runs the same code as the worker's nightly backup (app/services/backup.py), so it uses the same
# BACKUP_* settings from /opt/outreach/.env and needs no AWS CLI. The worker already backs up every night
# (just after midnight New York time); use this before an upgrade, or from cron for one extra daily copy:
#
#   /etc/cron.d/outreach-backup:
#   30 13 * * * outreach /opt/outreach/deploy/backup.sh >> /opt/outreach/data/backup.log 2>&1
#
# Run by hand:  sudo -u outreach /opt/outreach/deploy/backup.sh
set -euo pipefail
cd /opt/outreach
exec .venv/bin/python -c '
from datetime import datetime, timezone
from app.services.backup import run_backup
print(datetime.now(timezone.utc).isoformat(timespec="seconds"), run_backup(datetime.now(timezone.utc)))
'
