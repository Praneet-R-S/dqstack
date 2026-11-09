"""Settings, read once from the environment.

Three environments, chosen by DQ_ENV:

    development   SQLite file, relaxed cookies, console email. The default.
    staging       PostgreSQL, secure cookies, real email if configured.
    production    As staging, and refuses to start with an unsafe setting.

Production refusing to start is deliberate. A server that boots with the default
secret key, or with insecure cookies, works perfectly until the day it matters,
and nothing about it looks wrong from the outside.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    v = _env(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


DEV_SECRET = "dev-only-not-a-secret-change-me"


@dataclass(frozen=True)
class Settings:
    env: str = "development"
    database_url: str = f"sqlite:///{ROOT / 'data' / 'dqstack-app.db'}"
    secret_key: str = DEV_SECRET
    # Sessions and sign-in, section 15.
    session_hours: int = 12
    lockout_failures: int = 5
    lockout_minutes: int = 15
    cookie_secure: bool = False
    cookie_name: str = "dq_session"
    # Where the server is reachable, used in invitation and reset links.
    public_url: str = "http://127.0.0.1:8130"
    # Email. With no SMTP host, messages are written to the log instead of sent.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    mail_from: str = "DQ-Stack <no-reply@dqstack.local>"
    # The company timezone default the specification fixes.
    default_timezone: str = "Asia/Kolkata"
    allowed_origins: tuple[str, ...] = field(default_factory=tuple)
    # Forecast jobs: in-process in development and tests; in child processes with
    # time limits (worker.py) in staging and production.
    inline_jobs: bool = True
    train_timeout_s: int = 4 * 3600
    nightly_timeout_s: int = 1800
    # Google and Microsoft sign-in (OpenID Connect). Each is on only when both its id
    # and secret are set. The tenant limits which Microsoft accounts may try:
    # "organizations" (any work or school account), "common", or one tenant id.
    google_client_id: str | None = None
    google_client_secret: str | None = None
    microsoft_client_id: str | None = None
    microsoft_client_secret: str | None = None
    microsoft_tenant: str = "organizations"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


def load() -> Settings:
    env = (_env("DQ_ENV", "development") or "development").lower()
    if env not in ("development", "staging", "production", "test"):
        raise RuntimeError(f"DQ_ENV must be development, staging, production or test, not {env!r}")
    secure_default = env in ("staging", "production")
    s = Settings(
        env=env,
        database_url=_env("DATABASE_URL", Settings.database_url) or Settings.database_url,
        secret_key=_env("DQ_SECRET_KEY", DEV_SECRET) or DEV_SECRET,
        session_hours=int(_env("DQ_SESSION_HOURS", "12") or 12),
        lockout_failures=int(_env("DQ_LOCKOUT_FAILURES", "5") or 5),
        lockout_minutes=int(_env("DQ_LOCKOUT_MINUTES", "15") or 15),
        cookie_secure=_bool("DQ_COOKIE_SECURE", secure_default),
        public_url=(_env("DQ_PUBLIC_URL", "http://127.0.0.1:8130") or "").rstrip("/"),
        smtp_host=_env("DQ_SMTP_HOST"),
        smtp_port=int(_env("DQ_SMTP_PORT", "587") or 587),
        smtp_user=_env("DQ_SMTP_USER"),
        smtp_password=_env("DQ_SMTP_PASSWORD"),
        mail_from=_env("DQ_MAIL_FROM", Settings.mail_from) or Settings.mail_from,
        allowed_origins=tuple(o.strip() for o in (_env("DQ_ALLOWED_ORIGINS", "") or "").split(",")
                              if o.strip()),
        inline_jobs=_bool("DQ_INLINE_JOBS", env in ("development", "test")),
        train_timeout_s=int(_env("DQ_TRAIN_TIMEOUT_S", str(4 * 3600)) or 4 * 3600),
        nightly_timeout_s=int(_env("DQ_NIGHTLY_TIMEOUT_S", "1800") or 1800),
        google_client_id=_env("DQ_GOOGLE_CLIENT_ID"),
        google_client_secret=_env("DQ_GOOGLE_CLIENT_SECRET"),
        microsoft_client_id=_env("DQ_MICROSOFT_CLIENT_ID"),
        microsoft_client_secret=_env("DQ_MICROSOFT_CLIENT_SECRET"),
        microsoft_tenant=_env("DQ_MICROSOFT_TENANT", "organizations") or "organizations",
    )
    if env == "production":
        problems = []
        if s.secret_key == DEV_SECRET or len(s.secret_key) < 32:
            problems.append("DQ_SECRET_KEY must be set to at least 32 random characters")
        if s.is_sqlite:
            problems.append("DATABASE_URL must point at PostgreSQL, not SQLite")
        if not s.cookie_secure:
            problems.append("DQ_COOKIE_SECURE must be on (the site must be served over HTTPS)")
        if not s.public_url.startswith("https://"):
            problems.append("DQ_PUBLIC_URL must be an https:// address")
        if problems:
            raise RuntimeError("Refusing to start in production:\n  - " + "\n  - ".join(problems))
    return s


def generate_secret() -> str:
    """For the deployment guide: `python -m dqstack.app.config` prints one."""
    return secrets.token_urlsafe(48)


settings = load()

if __name__ == "__main__":
    print(generate_secret())
