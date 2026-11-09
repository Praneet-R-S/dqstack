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
