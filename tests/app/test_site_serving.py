"""The public site as the server delivers it.

Social previews need absolute image URLs, which a static file cannot know, so the
server fills them in from DQ_PUBLIC_URL. People who reach an address that does not
exist get the designed not-found page; programs calling /api/ get a plain sentence.
"""

from __future__ import annotations

import pytest

from dqstack.app import config

pytestmark = pytest.mark.skipif(not (config.ROOT / "site" / "index.html").exists(),
                                reason="the public site is not in this checkout")


def test_social_and_canonical_urls_are_absolute(client):
    base = config.settings.public_url.rstrip("/")
    home = client.get("/").text
    assert f'content="{base}/site-assets/og.png"' in home
    assert f'<link rel="canonical" href="{base}/">' in home
    privacy = client.get("/privacy.html").text
    assert f'<meta property="og:url" content="{base}/privacy.html">' in privacy


def test_unknown_addresses_get_the_designed_not_found_page(client):
    for path in ("/no/such/page", "/nope.html", "/404.html"):
        r = client.get(path)
        assert r.status_code == 404 and "text/html" in r.headers["content-type"], path
    r = client.get("/api/no-such-thing")
    assert r.status_code == 404 and r.json() == {"error": "That page or address does not exist."}


def test_pages_cannot_escape_the_site_folder(client):
    assert client.get("/..%2Fdqstack%2Fapp%2Fconfig.html").status_code == 404


def test_app_files_are_revalidated_so_updates_reach_every_device(client):
    for path in ("/app/js/core.js", "/app/css/app.css", "/app/"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers.get("cache-control") == "no-cache", path
    assert client.get("/api/health").headers.get("cache-control") == "no-store"


def _policy(resp) -> dict[str, str]:
    raw = resp.headers["Content-Security-Policy"]
    return {d.split()[0]: d for d in (p.strip() for p in raw.split(";")) if d}


def test_every_area_has_a_content_security_policy(client):
    """No inline script anywhere except the public pages' own, each allowed by its
    hash; the planner alone may load DuckDB-WASM from jsDelivr; nothing may frame us."""
    import base64
    import hashlib
    import re
    for path in ("/", "/privacy.html", "/terms.html", "/no-such-page"):
        r = client.get(path)
        p = _policy(r)
        assert "'unsafe-inline'" not in p["script-src"] and "jsdelivr" not in p["script-src"]
        bodies = [b for a, b in re.findall(r"<script(\s[^>]*)?>(.*?)</script>", r.text, re.S)
                  if not a or ("src=" not in a and "ld+json" not in a)]
        assert bodies, path
        for b in bodies:                          # the page's own inline scripts still run
            h = base64.b64encode(hashlib.sha256(b.encode()).digest()).decode()
            assert f"'sha256-{h}'" in p["script-src"], path
    app = _policy(client.get("/app/"))
    assert app["script-src"] == "script-src 'self'" and app["connect-src"] == "connect-src 'self'"
    planner = _policy(client.get("/planner/"))
    assert "https://cdn.jsdelivr.net" in planner["script-src"]
    assert "'wasm-unsafe-eval'" in planner["script-src"] and "blob:" in planner["worker-src"]
    api = _policy(client.get("/api/health"))
    assert api["default-src"] == "default-src 'none'"
    for p in (app, planner, api):
        assert p["frame-ancestors"] == "frame-ancestors 'none'"
