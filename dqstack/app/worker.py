"""The scheduler: one process that does the timed work for every company.

Every minute, in each company's own time zone:

  * close any store day nobody closed, flagged "closed automatically";
  * the nightly forecast, from 01:00 (section 9, "overnight refresh 01:00 to 03:00");
  * the weekly retrain, Sunday night from 02:00;
  * onboarding training as soon as there are 8 weeks of sales;
  * an early retrain when monitoring has asked for one;
  * any run an Owner or Admin queued by hand.

A failed night is not retried the same night: the next night's run tries again and,
meanwhile, yesterday's forecast stays on screen with its age (section 13). Jobs run
in child processes with time limits, so a crash or a runaway job cannot stop the
scheduler. Only one worker acts at a time: on PostgreSQL the others wait on an
advisory lock.

    python -m dqstack.app.worker            # forever
    python -m dqstack.app.worker --once     # a single pass
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import sales as sales_mod
from .config import settings
from .db import SessionLocal, engine, utcnow
from .forecast import jobs
from .models import Company, DailySales, ForecastRun
from .security import _aware

log = logging.getLogger("dqstack.worker")
LOCK_KEY = 72_011_026          # any constant; one advisory lock for the scheduler


def run_job(kind: str, company_id: str, trigger: str, run_id: str | None = None) -> str:
    """Run one job and return its final status."""
    if settings.inline_jobs:
        with SessionLocal() as db:
            fn = jobs.train if kind == "train" else jobs.nightly
            return fn(db, company_id, trigger, run_id).status
    timeout = settings.train_timeout_s if kind == "train" else settings.nightly_timeout_s
    tmp = tempfile.mkdtemp(prefix=f"dq-{kind}-")
    env = {**os.environ, "DQ_ARTIFACTS_DIR": f"{tmp}/artifacts", "DQ_INTERIM_DIR": f"{tmp}/interim",
           "DQ_PROCESSED_DIR": f"{tmp}/processed"}
    cmd = [sys.executable, "-m", "dqstack.app.forecast.jobs", kind, "--company", company_id,
           "--trigger", trigger] + (["--run", run_id] if run_id else [])
    try:
        done = subprocess.run(cmd, env=env, timeout=timeout, capture_output=True, text=True)
        if done.returncode != 0:
            log.warning("%s for %s exited %s: %s", kind, company_id, done.returncode,
                        done.stdout[-500:] + done.stderr[-1500:])
        return "succeeded" if done.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        log.error("%s for %s ran past %ss and was stopped", kind, company_id, timeout)
        with SessionLocal() as db:
            _mark_stuck(db, company_id, kind)
        return "failed"


def _mark_stuck(db: Session, company_id: str, kind: str) -> None:
    for r in db.scalars(select(ForecastRun).where(ForecastRun.company_id == company_id,
                                                  ForecastRun.kind == kind,
                                                  ForecastRun.status == "running")):
        jobs._fail(db, r, TimeoutError(f"{kind} ran past its time limit"), kind)


def _since(db: Session, company_id: str, kind: str, start: datetime) -> bool:
    return bool(db.scalar(select(func.count()).select_from(ForecastRun).where(
        ForecastRun.company_id == company_id, ForecastRun.kind == kind,
        ForecastRun.created_at >= start)))


def due(db: Session, co: Company, now: datetime) -> list[tuple[str, str, str | None]]:
    """What should run for this company now: (kind, trigger, queued run id)."""
    out: list[tuple[str, str, str | None]] = []
    for r in db.scalars(select(ForecastRun).where(ForecastRun.company_id == co.id,
                                                  ForecastRun.status == "queued")
                        .order_by(ForecastRun.created_at)):
        out.append((r.kind, r.trigger, r.id))
    if out:
        return out
    tz = ZoneInfo(co.timezone or "Asia/Kolkata")
    local = now.astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    active = jobs.active_version(db, co.id)
    if active is None:
        lo, hi = db.execute(select(func.min(DailySales.day), func.max(DailySales.day)).where(
            DailySales.company_id == co.id)).one()
        enough = lo is not None and (hi - lo).days + 1 >= jobs.MIN_DAYS
        if enough and not _since(db, co.id, "train", now - timedelta(hours=24)):
            out.append(("train", "onboarding", None))
        return out
    asked = (co.setup or {}).get("retrain_requested")
    if asked and not _since(db, co.id, "train", datetime.fromisoformat(asked)):
        out.append(("train", "early_retrain", None))
    elif local.weekday() == 6 and local.hour >= 2 and \
            not _since(db, co.id, "train", now - timedelta(days=6)):
        out.append(("train", "weekly", None))
    elif local.hour >= 1 and not _since(db, co.id, "nightly", midnight):
        out.append(("nightly", "schedule", None))
    return out


def tick(now: datetime | None = None) -> list[tuple[str, str, str]]:
    """One pass over every company. Returns what ran: (company, kind, status)."""
    now = now or utcnow()
    ran = []
    with SessionLocal() as db:
        sales_mod.auto_close(db, now)
        db.commit()
        companies = list(db.scalars(select(Company).where(Company.status == "active",
                                                          Company.is_demo.is_(False))))
        todo = [(co.id, item) for co in companies for item in due(db, co, now)]
    for company_id, (kind, trigger, run_id) in todo:
        status = run_job(kind, company_id, trigger, run_id)
        ran.append((company_id, kind, status))
        if kind == "train" and trigger != "manual":
            break                  # one training per pass keeps the nights predictable
    return ran


def _lock() -> bool:
    if engine().dialect.name != "postgresql":
        return True
    conn = engine().connect()
    got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar()
    if got:
        _lock.conn = conn               # held for the life of the process
    else:
        conn.close()
    return bool(got)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dqstack.app.worker")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--every", type=int, default=60, help="seconds between passes")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    while not _lock():
        log.info("another worker holds the scheduler lock; waiting")
        time.sleep(args.every)
    while True:
        try:
            for company, kind, status in tick():
                log.info("%s %s: %s", company, kind, status)
        except Exception:  # noqa: BLE001 -- the scheduler itself must keep going
            log.exception("scheduler pass failed")
        if args.once:
            return 0
        time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main())
