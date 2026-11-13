#!/bin/sh
# Start DQ-Stack in a Hugging Face Space: bring the database up to date, keep the
# scheduler running, create the operator and the demo company when asked, then serve.
# Settings come from the Space's Settings > Variables and secrets (deploy/HUGGINGFACE.md).
set -e

: "${DATABASE_URL:?Add DATABASE_URL (the Neon connection string) under Settings > Variables and secrets}"
: "${DQ_SECRET_KEY:?Add DQ_SECRET_KEY (at least 32 random characters) under Settings > Variables and secrets}"

# The Space's own https address, which Hugging Face passes in as SPACE_HOST.
if [ -z "$DQ_PUBLIC_URL" ] && [ -n "$SPACE_HOST" ]; then
  export DQ_PUBLIC_URL="https://$SPACE_HOST"
fi

python -m dqstack.app.manage migrate

# The scheduler: nightly forecasts, alerts, summaries. Restarted if it ever stops.
( while true; do
    python -m dqstack.app.worker || true
    echo "scheduler stopped; starting it again in 10 s" >&2
    sleep 10
  done ) &

# The operator's account for /platform, once (a second run says it exists, harmlessly).
if [ -n "$DQ_ADMIN_EMAIL" ] && [ -n "$DQ_ADMIN_PASSWORD" ]; then
  python -m dqstack.app.manage create-platform-admin --email "$DQ_ADMIN_EMAIL" \
    --name "Operations" || true
fi

# The demo company, once, in the background: its first forecast takes a few minutes.
if [ -n "$DQ_DEMO_EMAIL" ] && [ -n "$DQ_DEMO_PASSWORD" ]; then
  ( python -m dqstack.app.manage seed-demo --email "$DQ_DEMO_EMAIL" || true ) &
fi

exec uvicorn dqstack.app.main:app --host 0.0.0.0 --port "${PORT:-7860}" \
  --workers "${WEB_CONCURRENCY:-2}" --proxy-headers --forwarded-allow-ips '*'
