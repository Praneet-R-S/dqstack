"""Build one self-contained HTML file of the landing page, to send to people.

    python scrollcraft/builds/dqstack-dive/standalone.py

Everything is inlined: the stylesheet, the script, and every image as a data URI. Only
the Geist font still comes from Google Fonts (the page falls back to the system font
offline). Links into the app have nowhere to go outside the server, so they point at
the page's own sections instead.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITE = HERE.parents[2] / "site"
OUT = HERE / "dist" / "dqstack-landing.html"


def data_uri(rel: str) -> str:
    path = SITE / "assets" / rel
    mime = {"webp": "image/webp", "png": "image/png", "svg": "image/svg+xml", "jpg": "image/jpeg"}[path.suffix[1:]]
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def inline_assets(text: str) -> str:
    # Use the smaller screen captures only; a srcset of data URIs would double the file.
    text = re.sub(r'\s+srcset="[^"]*"\s+sizes="[^"]*"', "", text)
    return re.sub(r"/site-assets/([\w/.-]+\.(?:webp|png|svg|jpg))",
                  lambda m: data_uri(m.group(1)), text)


def main() -> None:
    html = (SITE / "index.html").read_text(encoding="utf-8")
    css = (SITE / "assets" / "css" / "dive.css").read_text(encoding="utf-8")
    js = (SITE / "assets" / "js" / "dive.js").read_text(encoding="utf-8")

    html = html.replace('<link rel="stylesheet" href="/site-assets/css/dive.css">',
                        "<style>\n" + inline_assets(css) + "\n</style>")
    html = html.replace('<script src="/site-assets/js/dive.js" defer></script>',
                        "<script>\n" + inline_assets(js) + "\n</script>")
    # Preloads and social cards point at server files; not needed in a file.
    html = re.sub(r'\s*<link rel="preload"[^>]*>', "", html)
    html = re.sub(r'\s*<meta (?:property|name)="(?:og|twitter):image[^"]*"[^>]*>', "", html)
    html = re.sub(r'\s*<link rel="(?:icon|apple-touch-icon)"[^>]*>', "", html)

    # App links and other pages: keep the visitor on this page.
    html = re.sub(r'href="/app/#/signup"', 'href="#ready"', html)
    html = re.sub(r'href="/app/#/signin"', 'href="#ready"', html)
    html = re.sub(r'href="/(privacy|terms)\.html"', 'href="#questions"', html)
    html = html.replace('href="/"', 'href="#dive"')

    html = inline_assets(html)
    leftovers = sorted(set(re.findall(r"/site-assets/[^\"')\s]+", html)))
    assert not leftovers, leftovers

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"{OUT}  {OUT.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
