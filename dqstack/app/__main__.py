"""Run the application server for development:

    python -m dqstack.app [--port 8130] [--host 127.0.0.1]

Brings a development SQLite database up to date first, so a fresh checkout runs
with one command. Staging and production run migrations as a separate deploy step
(deploy/README.md) and serve with gunicorn; this entry point refuses to auto-migrate
them.
"""

from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m dqstack.app")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8130")))
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()
    from .config import settings
    if settings.env == "development" and settings.is_sqlite:
        from .manage import cmd_migrate
        cmd_migrate(None)
    uvicorn.run("dqstack.app.main:app", host=args.host, port=args.port, reload=args.reload,
                proxy_headers=True)


if __name__ == "__main__":
    main()
