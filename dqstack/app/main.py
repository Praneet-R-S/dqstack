"""The DQ-Stack application server.

    uvicorn dqstack.app.main:app                     # development
    gunicorn -k uvicorn.workers.UvicornWorker ...    # production (see deploy/)

Errors are answered in plain language and never with a stack trace (interface rule
6). Every response carries security headers. The planner interface the forecasting
engine already had is served at /planner, the store-operations app at /app, and the
public site at /.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings
from .db import engine
from .routes import auth, org

log = logging.getLogger("dqstack.app")
ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SITE = ROOT / "site"
APP_UI = ROOT / "webapp"

VERSION = "1.0.0"
PLAIN = {404: "That page or address does not exist.",
         405: "That action is not available here."}


def site_page(name: str, status: int = 200) -> HTMLResponse:
    """A public page with its social-card and canonical URLs made absolute from
    DQ_PUBLIC_URL, which link previews require and a static file cannot know."""
    html = (SITE / name).read_text(encoding="utf-8")
    base = settings.public_url.rstrip("/")
    html = html.replace('content="/site-assets/', f'content="{base}/site-assets/')
    if status == 200 and 'rel="canonical"' not in html:
        url = f"{base}/" if name == "index.html" else f"{base}/{name}"
        tags = (f'  <link rel="canonical" href="{url}">\n'
                f'  <meta property="og:url" content="{url}">\n')
        html = html.replace("</head>", tags + "</head>", 1)
    return HTMLResponse(html, status_code=status)


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        t0 = time.perf_counter()
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
        if settings.cookie_secure:
            resp.headers.setdefault("Strict-Transport-Security",
                                    "max-age=31536000; includeSubDomains")
        if request.url.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - t0) * 1000:.1f}"
        return resp


def create_app() -> FastAPI:
    app = FastAPI(title="DQ-Stack", version=VERSION, docs_url="/api/docs",
                  redoc_url=None, openapi_url="/api/openapi.json")
    app.add_middleware(SecurityHeaders)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        # Covers our own HTTPExceptions and the router's "no such path" alike. People
        # get the designed not-found page; programs get a plain JSON sentence.
        if exc.status_code == 404 and not request.url.path.startswith("/api/")                 and (SITE / "404.html").exists():
            return site_page("404.html", 404)
        detail = exc.detail
        if detail in ("Not Found", "Method Not Allowed", None, ""):
            detail = PLAIN.get(exc.status_code, "That request could not be completed.")
        return JSONResponse({"error": detail}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        problems = []
        for e in exc.errors():
            field = ".".join(str(x) for x in e.get("loc", [])[1:]) or "request"
            problems.append(f"{field}: {e.get('msg', 'invalid')}")
        return JSONResponse({"error": "Some details need fixing.", "problems": problems},
                            status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"error": "Something went wrong on our side. Your work was not "
                                      "saved; please try again. If it keeps happening, tell "
                                      "your administrator."}, status_code=500)

    @app.get("/api/health")
    def health():
        """Liveness and database reachability, for the load balancer and monitoring."""
        try:
            with engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            db_ok = True
        except Exception:  # noqa: BLE001
            db_ok = False
        return JSONResponse({"ok": db_ok, "version": VERSION, "env": settings.env},
                            status_code=200 if db_ok else 503)

    app.include_router(auth.router)
    app.include_router(org.router)
    _mount_later_routers(app)

    # ---- static surfaces
    if UI.exists():
        app.mount("/planner", StaticFiles(directory=UI, html=True), name="planner")
    if APP_UI.exists():
        @app.get("/platform", include_in_schema=False)
        def platform_portal():
            return RedirectResponse("/app/platform.html")

        app.mount("/app", StaticFiles(directory=APP_UI, html=True), name="app")
    if SITE.exists():
        app.mount("/site-assets", StaticFiles(directory=SITE / "assets"), name="site-assets") \
            if (SITE / "assets").exists() else None

        @app.get("/", include_in_schema=False)
        def home():
            return site_page("index.html")

        @app.get("/{page}.html", include_in_schema=False)
        def public_page(page: str):
            f = SITE / f"{page}.html"
            if page != "404" and f.exists() and f.resolve().parent == SITE.resolve():
                return site_page(f.name)
            return site_page("404.html", 404)
    else:
        @app.get("/", include_in_schema=False)
        def home_redirect():
            return RedirectResponse("/app/")

    return app


def _mount_later_routers(app: FastAPI) -> None:
    """Routers for later build steps register here as they are added."""
    import importlib
    for name in ("setup", "calendar", "catalogue", "imports", "stock", "floor", "sales", "today",
                 "orders", "checkout", "pos", "forecast", "alerts", "reports", "platform"):
        try:
            mod = importlib.import_module(f".routes.{name}", package=__package__)
        except ModuleNotFoundError as e:
            if e.name and e.name.endswith(f"routes.{name}"):
                continue
            raise
        app.include_router(mod.router)


app = create_app()
