"""Operations commands for a deployed server.

    python -m dqstack.app.manage check               settings, database, migrations
    python -m dqstack.app.manage migrate             apply database migrations
    python -m dqstack.app.manage secret              print a new DQ_SECRET_KEY
    python -m dqstack.app.manage create-platform-admin --email ops@vendor.in --name "Ops"
    python -m dqstack.app.manage sftp-add-folder --folder acme-pos --api-key-id ID \\
                                                 --public-key "ssh-ed25519 AAAA... pos@acme"
    python -m dqstack.app.manage sftp-remove-folder --folder acme-pos

`create-platform-admin` reads the password from DQ_ADMIN_PASSWORD, or asks for it
when run in a terminal. It is never accepted on the command line, where it would be
kept in shell history and visible to other users in the process list.

`sftp-add-folder` gives a billing system its own SFTP folder for the nightly file
drop, acting as one API key (filedrop.py; deploy/README.md section 13). Run it in the
worker container as root, which owns the drop's folders:
    docker compose run --rm --user root worker manage sftp-add-folder ...
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _alembic_cfg(url: str | None = None):
    from alembic.config import Config

    from .config import settings
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url or settings.database_url)
    cfg.attributes["configure_logger"] = False
    return cfg


def cmd_migrate(_args) -> int:
    from alembic import command

    from .config import settings
    if settings.is_sqlite:
        (ROOT / "data").mkdir(exist_ok=True)
    command.upgrade(_alembic_cfg(), "head")
    print("Database is up to date.")
    return 0


def cmd_check(_args) -> int:
    """Exit 0 only when the server is safe to put in front of people."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from .config import settings  # raises in production if anything is unsafe
    url = make_url(settings.database_url)
    print(f"environment     {settings.env}")
    print(f"database        {url.get_backend_name()} {url.host or ''}/{url.database or ''}")
    print(f"public address  {settings.public_url}")
    print(f"secure cookies  {'on' if settings.cookie_secure else 'OFF'}")
    print(f"email           {'SMTP ' + settings.smtp_host if settings.smtp_host else 'log only (no SMTP host)'}")
    ok = True
    try:
        eng = create_engine(settings.database_url, future=True)
        with eng.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
        eng.dispose()
    except Exception as e:  # noqa: BLE001
        print(f"database        UNREACHABLE: {e.__class__.__name__}")
        return 1
    head = ScriptDirectory.from_config(_alembic_cfg()).get_current_head()
    print(f"migrations      {current or 'none'} (latest {head})")
    if current != head:
        print("  run: python -m dqstack.app.manage migrate")
        ok = False
    if settings.env in ("staging", "production") and not settings.smtp_host:
        print("  warning: invitations and password resets will not be emailed")
    return 0 if ok else 1


def cmd_secret(_args) -> int:
    from .config import generate_secret
    print(generate_secret())
    return 0


def cmd_create_platform_admin(args) -> int:
    from sqlalchemy import select

    from .db import SessionLocal
    from .models import Person
    from .security import hash_password, password_problem

    password = os.environ.get("DQ_ADMIN_PASSWORD")
    if not password:
        if not sys.stdin.isatty():
            print("Set DQ_ADMIN_PASSWORD or run this in a terminal.", file=sys.stderr)
            return 2
        password = getpass.getpass("Password for the platform administrator: ")
        if password != getpass.getpass("Same password again: "):
            print("The two passwords differ.", file=sys.stderr)
            return 2
    problem = password_problem(password)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    email = args.email.strip().lower()
    with SessionLocal() as db:
        if db.scalar(select(Person.id).where(Person.email == email)):
            print("Someone with that email already has an account.", file=sys.stderr)
            return 1
        db.add(Person(company_id=None, name=args.name.strip(), email=email, role="admin",
                      status="active", is_platform_admin=True,
                      password_hash=hash_password(password)))
        db.commit()
    print(f"Platform administrator {email} created. Sign in at /platform.")
    return 0


def cmd_seed_demo(args) -> int:
    """A demonstration company with synthetic data and a first forecast."""
    from .db import SessionLocal
    from .demo import seed
    from .forecast import jobs
    password = os.environ.get("DQ_DEMO_PASSWORD")
    if not password:
        if not sys.stdin.isatty():
            print("Set DQ_DEMO_PASSWORD or run this in a terminal.", file=sys.stderr)
            return 2
        password = getpass.getpass("Password for every demo account: ")
    from .security import password_problem
    if (problem := password_problem(password)):
        print(problem, file=sys.stderr)
        return 2
    with SessionLocal() as db:
        try:
            co = seed(db, args.email, password, days=args.days)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        db.commit()
        print(f"Made {co.name}. Training the first forecast (a few minutes)...")
        run = jobs.train(db, co.id, trigger="onboarding")
        print(f"Training {run.status}: {run.message}")
    print(f"Sign in at /app/ as {args.email} (Owner). The team: meera@, arjun@, puja@, "
          "kabir@ and chirag@demo.dqstack.local, same password. Manager PIN 2580.")
    return 0


def cmd_sftp_add_folder(args) -> int:
    from . import filedrop
    r = filedrop.root()
    if r is None:
        print("Set DQ_SFTP_ROOT to the file drop's folder (in docker-compose.yml the worker "
              "has it).", file=sys.stderr)
        return 2
    try:
        key = (Path(args.public_key_file).read_text(encoding="utf-8")
               if args.public_key_file else args.public_key)
        info = filedrop.add_folder(r, args.folder, args.api_key_id, key)
    except (OSError, filedrop.FolderError) as e:
        print(e, file=sys.stderr)
        return 1
    limited = " (limited to one store)" if info["store_id"] else ""
    print(f"Folder {info['folder']} now records files for {info['company']} with the API key "
          f"'{info['key_name']}' ({info['key_prefix']}...){limited}.")
    print("Let the account log in by recreating the SFTP service:")
    print("  docker compose --profile sftp up -d --force-recreate sftp")
    print(f"The billing system uploads CSV files to sftp://{info['folder']}@YOUR-DOMAIN:"
          f"{os.environ.get('DQ_SFTP_PORT') or 2222}/upload/ with its private key.")
    return 0


def cmd_sftp_remove_folder(args) -> int:
    from . import filedrop
    r = filedrop.root()
    if r is None:
        print("Set DQ_SFTP_ROOT to the file drop's folder.", file=sys.stderr)
        return 2
    if not filedrop.remove_folder(r, args.folder):
        print(f"There is no folder {args.folder}.", file=sys.stderr)
        return 1
    print(f"Folder {args.folder} is no longer read; its files are left in place. Recreate the "
          "SFTP service to stop its account logging in:")
    print("  docker compose --profile sftp up -d --force-recreate sftp")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m dqstack.app.manage")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="check settings, database and migrations")
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("secret", help="print a new secret key")
    a = sub.add_parser("create-platform-admin", help="create the vendor's support account")
    a.add_argument("--email", required=True)
    a.add_argument("--name", required=True)
    d = sub.add_parser("seed-demo", help="make a demonstration company with a first forecast")
    d.add_argument("--email", required=True)
    d.add_argument("--days", type=int, default=330)
    f = sub.add_parser("sftp-add-folder",
                       help="give a billing system an SFTP folder for the nightly file drop")
    f.add_argument("--folder", required=True, help="the SFTP account and folder name")
    f.add_argument("--api-key-id", required=True,
                   help="the API key the folder's files are recorded with: its id, or the "
                        "dqk_... beginning shown under Settings > Integrations")
    keys = f.add_mutually_exclusive_group(required=True)
    keys.add_argument("--public-key", help="the billing system's OpenSSH public key, one line")
    keys.add_argument("--public-key-file", help="a file holding that public key")
    r = sub.add_parser("sftp-remove-folder", help="stop reading an SFTP folder")
    r.add_argument("--folder", required=True)
    args = p.parse_args(argv)
    try:
        from . import config  # noqa: F401  -- production refuses unsafe settings here
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    return {"check": cmd_check, "migrate": cmd_migrate, "secret": cmd_secret,
            "create-platform-admin": cmd_create_platform_admin,
            "seed-demo": cmd_seed_demo, "sftp-add-folder": cmd_sftp_add_folder,
            "sftp-remove-folder": cmd_sftp_remove_folder}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
