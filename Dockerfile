# DQ-Stack application image: the API server, the store app, the public site, the
# planner, and the forecasting engine (which the worker runs).
#
#   docker build -t dqstack .
#
# torch comes from the CPU-only index: the engine needs it for NHITS, and the default
# Linux wheel would add several gigabytes of CUDA libraries the server never uses.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DQ_ENV=production TZ=Asia/Kolkata

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 tzdata curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/dqstack
COPY requirements.txt requirements-app.txt constraints.txt ./
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch==2.15.0+cpu" \
 && pip install -c constraints.txt -r requirements.txt -r requirements-app.txt

COPY dqstack ./dqstack
COPY ui ./ui
COPY webapp ./webapp
COPY site ./site
COPY alembic.ini ./
COPY deploy/entrypoint.sh /usr/local/bin/dq-entrypoint
RUN chmod +x /usr/local/bin/dq-entrypoint \
 && useradd --create-home --uid 10001 dqstack \
 && mkdir -p /srv/dqstack/data /srv/dqstack/artifacts /srv/dqstack/ui/data \
 && chown -R dqstack:dqstack /srv/dqstack/data /srv/dqstack/artifacts /srv/dqstack/ui/data
USER dqstack

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1
ENTRYPOINT ["dq-entrypoint"]
CMD ["web"]
