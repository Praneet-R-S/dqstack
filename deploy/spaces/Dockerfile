# DQ-Stack on a Hugging Face Space (Docker SDK): the application and the scheduler in
# one container, PostgreSQL elsewhere (Neon). Published by scripts/deploy_hf.py, which
# puts this file at the Space's root beside the code. deploy/HUGGINGFACE.md has the steps.
#
# Differences from the main Dockerfile: Spaces run the container as user 1000 and send
# traffic to port 7860, and there is no docker-compose, so start.sh runs the migrations,
# the scheduler and the web server together.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DQ_ENV=production TZ=Asia/Kolkata PORT=7860 WEB_CONCURRENCY=2 \
    DQ_DB_POOL_SIZE=5 DQ_DB_MAX_OVERFLOW=5

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 tzdata curl \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 user
WORKDIR /home/user/app

COPY --chown=user requirements.txt requirements-app.txt constraints.txt ./
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch==2.15.0+cpu" \
 && pip install -c constraints.txt -r requirements.txt -r requirements-app.txt

COPY --chown=user dqstack ./dqstack
COPY --chown=user ui ./ui
COPY --chown=user webapp ./webapp
COPY --chown=user site ./site
COPY --chown=user alembic.ini start.sh ./
RUN mkdir -p data artifacts ui/data \
 && chown -R user:user /home/user/app \
 && chmod +x start.sh

USER user
EXPOSE 7860
CMD ["./start.sh"]
