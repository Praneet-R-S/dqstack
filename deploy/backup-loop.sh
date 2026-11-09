#!/bin/sh
# Nightly database dump, kept for 30 days (docker-compose.yml "backup" service).
# Runs inside the postgres image. A dump is written to a .part file first and renamed
# only when complete, so a half-written file is never mistaken for a backup.
#
# This is the second, independent copy: a daily restore point in a different format
# from a different tool. Point-in-time restore (any moment in the last 7 days, at most
# about 5 minutes lost) comes from the pitr service's WAL-G archive; see
# deploy/README.md section 5. The time of day is in TZ (Asia/Kolkata by default).
set -eu
KEEP_DAYS="${KEEP_DAYS:-30}"
while true; do
  today="$(date +%F)"
  now="$(date +%H%M)"
  if [ "$now" -ge 0230 ] && [ ! -f "/backups/.done-$today" ]; then
    file="/backups/dqstack-$today-$now.dump"
    if pg_dump -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f "$file.part"; then
      mv "$file.part" "$file"
      touch "/backups/.done-$today"
      echo "backup written: $file"
    else
      echo "backup FAILED at $(date)" >&2
    fi
    # Old dumps and markers only; the pattern never matches anything else.
    find /backups -maxdepth 1 -type f -name 'dqstack-*.dump' -mtime "+$KEEP_DAYS" -exec rm -f {} \;
    find /backups -maxdepth 1 -type f -name '.done-*' -mtime +2 -exec rm -f {} \;
  fi
  sleep 600
done
