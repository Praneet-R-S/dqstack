#!/bin/sh
# web       the API server, store app, site and planner (uvicorn, several processes)
# worker    the scheduler: nightly forecasts, weekly retraining, day close, alerts
# migrate   bring the database up to date (run once per release, as the owner role)
# manage    any manage.py command, e.g. "manage create-platform-admin --email ..."
set -e
case "${1:-web}" in
  web)
    shift || true
    exec uvicorn dqstack.app.main:app --host 0.0.0.0 --port 8000 \
      --workers "${WEB_CONCURRENCY:-2}" --proxy-headers --forwarded-allow-ips '*' "$@" ;;
  worker)
    exec python -m dqstack.app.worker ;;
  migrate)
    exec python -m dqstack.app.manage migrate ;;
  manage)
    shift
    exec python -m dqstack.app.manage "$@" ;;
  *)
    exec "$@" ;;
esac
