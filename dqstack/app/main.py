"""The DQ-Stack application server.

    uvicorn dqstack.app.main:app                     # development
    uvicorn ... --workers N                          # production (deploy/entrypoint.sh)

Errors are answered in plain language and never with a stack trace (interface rule
6). Every response carries security headers. The planner interface the forecasting
engine already had is served at /planner, the store-operations app at /app, and the
public site at /.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

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
    resp = HTMLResponse(html, status_code=status)
    resp.headers["Content-Security-Policy"] = csp("site", inline_script_hashes(html))
    return resp


# Content-Security-Policy, one per area. No area allows inline script except the public
# pages' own small inline scripts, each allowed by its hash; styles may be inline
# (style attributes set by the screens), fonts come from Google Fonts, and the planner
# loads DuckDB-WASM from jsDelivr into a worker made from a blob.
_FONTS_CSS, _FONTS = "https://fonts.googleapis.com", "https://fonts.gstatic.com"
_JSDELIVR = "https://cdn.jsdelivr.net"
_BASE = ("default-src 'self'", f"style-src 'self' 'unsafe-inline' {_FONTS_CSS}",
         f"font-src 'self' {_FONTS} data:", "img-src 'self' data: blob:",
         "object-src 'none'", "base-uri 'self'", "form-action 'self'",
         "frame-ancestors 'none'")
_SCRIPT = {
    "site": ("script-src 'self'{hashes}", "connect-src 'self'"),
    "app": ("script-src 'self'", "connect-src 'self'", "worker-src 'self' blob:",
            "media-src 'self' blob:"),
    "planner": (f"script-src 'self' {_JSDELIVR} 'wasm-unsafe-eval'",
                f"connect-src 'self' {_JSDELIVR}", "worker-src 'self' blob:"),
    "api": ("default-src 'none'", "frame-ancestors 'none'"),
}


def csp(area: str, hashes: tuple[str, ...] = ()) -> str:
    if area == "api":
        return "; ".join(_SCRIPT["api"])
    extra = "".join(f" '{h}'" for h in hashes)
    return "; ".join([*_BASE, *(p.format(hashes=extra) for p in _SCRIPT[area])])


def inline_script_hashes(html: str) -> tuple[str, ...]:
    """sha256 sources for a page's inline executable scripts (not JSON-LD data)."""
    import base64
    import hashlib
    import re
    out = []
    for attrs, body in re.findall(r"<script(\s[^>]*)?>(.*?)</script>", html, re.S):
        attrs = attrs or ""
        if "src=" in attrs or "application/ld+json" in attrs:
            continue
        digest = hashlib.sha256(body.encode("utf-8")).digest()
        out.append("sha256-" + base64.b64encode(digest).decode("ascii"))
    return tuple(out)


def _csp_area(path: str) -> str:
    if path.startswith("/api/"):
        return "api"
    if path.startswith("/planner"):
        return "planner"
    if path.startswith(("/app", "/platform")):
        return "app"
    return "site"


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
        path = request.url.path
        # A page that set its own (site_page, with its inline scripts' hashes) keeps it.
        resp.headers.setdefault("Content-Security-Policy", csp(_csp_area(path)))
        if path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        elif path.startswith(("/app", "/planner", "/platform")) or path.endswith((".html", ".js", ".css"))                 or path == "/":
            # The app's own files: the browser keeps them but checks with the server (by
            # ETag) on every load, so an update reaches every till on its next page load.
            resp.headers.setdefault("Cache-Control", "no-cache")
        resp.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - t0) * 1000:.1f}"
        return resp


def _app_logging() -> None:
    """The app's own messages (warnings everywhere; in development also the mail and text
    messages that were not sent, so invitation links and codes can be read) go to the
    server's output beside uvicorn's. Uvicorn configures only its own loggers."""
    root = logging.getLogger("dqstack")
    if root.handlers:
        return
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(h)
    root.setLevel(logging.INFO)
    root.propagate = False


class Admission:
    """At most `n` API requests in progress per web process; the rest wait at the door.

    A request takes its database connection in its first dependency (the sign-in or
    API-key check) and keeps it until the response, including while it waits for a
    thread to run the endpoint. With 200 tills sending at once, hundreds of requests
    held connections that way, the pool ran dry, and requests that waited 30 seconds for
    one failed: lost sales, in the scale job. Bounding the requests in progress to what
    the pool can serve turns that into a short queue before any database work starts.
    Pages and files do not touch the database and are not held back."""

    def __init__(self, app, n: int | None):
        self.app, self.n, self.sem = app, n, None

    async def __call__(self, scope, receive, send):
        if self.n is None or scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        if self.sem is None:
            import asyncio
            self.sem = asyncio.Semaphore(self.n)
        async with self.sem:
            await self.app(scope, receive, send)


@asynccontextmanager
async def _lifespan(app):
    # Match the request threads to the database pool (db.request_threads explains why).
    from .db import request_threads
    n = request_threads()
    if n is not None:
        import anyio.to_thread
        anyio.to_thread.current_default_thread_limiter().total_tokens = n
    yield


def create_app() -> FastAPI:
    _app_logging()
    app = FastAPI(title="DQ-Stack", version=VERSION, docs_url="/api/docs",
                  redoc_url=None, openapi_url="/api/openapi.json", lifespan=_lifespan)
    app.add_middleware(SecurityHeaders)
    from .db import request_threads
    app.add_middleware(Admission, n=request_threads())     # outermost: before any DB work

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
        # The planner's SQL console (DuckDB-WASM) reads the demo's series as Arrow.
        # dqstack.serve has this endpoint; served from here too, or the planner at
        # /planner had no data for its in-browser queries. Built from the same bundle
        # the page reads, once per bundle file.
        arrow_cache: dict = {}

        @app.get("/planner/series.arrow", include_in_schema=False)
        def planner_series_arrow():
            from fastapi.responses import Response
            from ..config import UI_DATA
            bundle = UI_DATA / "bundle.json"
            if not bundle.exists():
                raise HTTPException(404, "The planner's demo data has not been exported.")
            key = bundle.stat().st_mtime_ns
            if arrow_cache.get("key") != key:
                from ..serve import series_table_arrow, to_ipc
                arrow_cache.update(key=key, body=to_ipc(series_table_arrow(bundle)))
            return Response(arrow_cache["body"],
                            media_type="application/vnd.apache.arrow.stream")

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
                 "orders", "checkout", "pos", "forecast", "alerts", "reports", "platform",
                 "connectors"):
        try:
            mod = importlib.import_module(f".routes.{name}", package=__package__)
        except ModuleNotFoundError as e:
            if e.name and e.name.endswith(f"routes.{name}"):
                continue
            raise
        app.include_router(mod.router)


app = create_app()
