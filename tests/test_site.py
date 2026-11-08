"""The public site in site/ must meet its ship requirements on every page.

These are the things that make a marketing page feel unfinished or untrustworthy
when they slip, and none of them shows up as an error in a browser: a missing
meta description, a second <h1>, a footer without the legal links, an og:image
that points at nothing, a banned typeface creeping back in through a font
stack, or a hyphen in copy that the design rules forbid. Each is cheap to check
mechanically and easy to break by hand.

Standard library and pytest only.
"""

from __future__ import annotations

import html.parser
import json
import pathlib
import re
import struct

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
ASSETS = SITE / "assets"
PAGES = sorted(SITE.glob("*.html"))
LEGAL_COMMENT = ("<!-- Draft written from the product specification. "
                 "Have a lawyer review before launch. -->")

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "source", "track", "wbr"}
BANNED_FONTS = re.compile(r"\b(inter|roboto|arial|helvetica)\b|open[\s+_-]?sans", re.IGNORECASE)
BANNED_FILLER = re.compile(
    r"revolutioni[sz]e|supercharge|cutting[\s-]edge|effortless|transform your workflow|"
    r"take it to the next level|harness the power|world[\s-]class", re.IGNORECASE)
# Hyphen, non-breaking hyphen, figure dash, en dash, em dash. The minus sign
# (U+2212) is allowed in figures; the product name keeps its hyphen.
DASHES = re.compile("[-‐‑‒–—]")
TEXT_META = {"description", "og:title", "og:description", "og:image:alt",
             "twitter:title", "twitter:description"}


class Page(html.parser.HTMLParser):
    """One pass over a page: tags, ids, visible text, JSON-LD and tag balance."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.ids: set[str] = set()
        self.text: list[str] = []
        self.title = ""
        self.json_ld: list[str] = []
        self.stack: list[tuple[str, int]] = []
        self.errors: list[str] = []
        self._raw: str | None = None          # inside <script> or <style>
        self._in_title = False

    def _record(self, tag: str, attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        a = {k: (v or "") for k, v in attrs}
        self.tags.append((tag, a))
        if a.get("id"):
            self.ids.add(a["id"])
        return a

    def handle_starttag(self, tag, attrs):
        a = self._record(tag, attrs)
        if tag in ("script", "style"):
            self._raw = "json" if a.get("type") == "application/ld+json" else tag
        if tag == "title":
            self._in_title = True
        if tag not in VOID:
            self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        self._record(tag, attrs)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._raw = None
        if tag == "title":
            self._in_title = False
        if tag in VOID:
            return
        line = self.getpos()[0]
        if not self.stack:
            self.errors.append(f"stray </{tag}> at line {line}")
        elif self.stack[-1][0] != tag:
            self.errors.append(f"</{tag}> at line {line} closes <{self.stack[-1][0]}> "
                               f"opened at line {self.stack[-1][1]}")
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag:
                    del self.stack[i:]
                    break
        else:
            self.stack.pop()

    def handle_data(self, data):
        if self._raw == "json":
            self.json_ld.append(data)
        elif self._raw:
            return
        elif self._in_title:
            self.title += data
        else:
            self.text.append(data)

    # ---- helpers
    def all(self, tag: str) -> list[dict[str, str]]:
        return [a for t, a in self.tags if t == tag]

    def meta(self, key: str) -> str | None:
        for a in self.all("meta"):
            if a.get("name") == key or a.get("property") == key:
                return a.get("content")
        return None

    def hrefs(self) -> list[str]:
        return [a["href"] for t, a in self.tags if t in ("a", "link") and "href" in a]


def parse(path: pathlib.Path) -> Page:
    page = Page()
    page.feed(path.read_text(encoding="utf-8"))
    page.close()
    return page


def page_ids(name: str) -> set[str]:
    return parse(SITE / name).ids


pages = pytest.mark.parametrize("path", PAGES, ids=[p.name for p in PAGES])


# ------------------------------------------------------------------ the set

def test_the_expected_pages_exist():
    names = {p.name for p in PAGES}
    for required in ("index.html", "privacy.html", "terms.html", "404.html"):
        assert required in names, f"site/{required} is missing"


def test_a_custom_404_exists():
    assert (SITE / "404.html").is_file()


# ------------------------------------------------------------ every page

@pages
def test_html_has_a_language(path):
    htmls = parse(path).all("html")
    assert htmls and htmls[0].get("lang"), f"{path.name}: <html> needs a lang attribute"


@pages
def test_has_a_title(path):
    assert parse(path).title.strip(), f"{path.name}: <title> is missing or empty"


@pages
def test_has_meta_description_and_viewport(path):
    page = parse(path)
    assert (page.meta("description") or "").strip(), f"{path.name}: no meta description"
    viewport = page.meta("viewport") or ""
    assert "width=device-width" in viewport, f"{path.name}: no responsive viewport meta"


@pages
def test_has_social_sharing_tags(path):
    page = parse(path)
    for key in ("og:title", "og:description", "og:image"):
        assert (page.meta(key) or "").strip(), f"{path.name}: {key} is missing"


@pages
def test_og_image_is_a_real_1200_by_630_png(path):
    src = parse(path).meta("og:image") or ""
    assert src.startswith("/site-assets/"), f"{path.name}: og:image should be a site asset, got {src!r}"
    file = ASSETS / src.removeprefix("/site-assets/")
    assert file.is_file(), f"{path.name}: og:image {src} does not exist"
    head = file.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{file.name} is not a PNG"
    width, height = struct.unpack(">II", head[16:24])
    assert (width, height) == (1200, 630), f"{file.name} is {width}x{height}, expected 1200x630"


@pages
def test_has_a_favicon_that_exists(path):
    icons = [a for a in parse(path).all("link") if a.get("rel") == "icon"]
    assert icons, f"{path.name}: no favicon link"
    for a in icons:
        assert (ASSETS / a["href"].removeprefix("/site-assets/")).is_file(), a["href"]


@pages
def test_has_a_working_skip_link_first(path):
    page = parse(path)
    links = page.all("a")
    assert links, f"{path.name}: no links at all"
    first = links[0]
    assert "skip-link" in first.get("class", "").split(), (
        f"{path.name}: the first link on the page should be the skip link")
    target = first.get("href", "")
    assert target.startswith("#") and target[1:] in page.ids, (
        f"{path.name}: skip link points at {target!r}, which is not an element on the page")


@pages
def test_has_exactly_one_h1(path):
    count = len(parse(path).all("h1"))
    assert count == 1, f"{path.name}: expected exactly one <h1>, found {count}"


@pages
def test_links_to_privacy_and_terms(path):
    hrefs = parse(path).hrefs()
    assert "/privacy.html" in hrefs, f"{path.name}: no link to /privacy.html"
    assert "/terms.html" in hrefs, f"{path.name}: no link to /terms.html"


@pages
def test_every_image_has_alt_text(path):
    for a in parse(path).all("img"):
        assert a.get("alt", "").strip(), f"{path.name}: <img src={a.get('src')!r}> has no alt text"


@pages
def test_inline_svg_is_hidden_or_labelled(path):
    for a in parse(path).all("svg"):
        hidden = a.get("aria-hidden") == "true"
        labelled = a.get("role") == "img" and (a.get("aria-label") or a.get("aria-labelledby"))
        assert hidden or labelled, f"{path.name}: an <svg class={a.get('class')!r}> is neither hidden nor labelled"


@pages
def test_internal_links_and_icon_references_resolve(path):
    page = parse(path)
    index_ids = page_ids("index.html")
    problems = []
    refs = page.hrefs() + [a["src"] for a in page.all("script") if a.get("src")]
    for ref in refs:
        if ref.startswith(("https://", "/app/", "/planner/")) or ref == "/":
            continue
        if ref.startswith("#"):
            if ref[1:] not in page.ids:
                problems.append(ref)
        elif ref.startswith("/#"):
            if ref[2:] not in index_ids:
                problems.append(ref)
        elif ref.startswith("/site-assets/"):
            if not (ASSETS / ref.removeprefix("/site-assets/")).is_file():
                problems.append(ref)
        elif re.fullmatch(r"/[\w-]+\.html", ref):
            if not (SITE / ref[1:]).is_file():
                problems.append(ref)
        else:
            problems.append(f"{ref} (unexpected form)")
    for a in page.all("use"):
        if a.get("href", "").lstrip("#") not in page.ids:
            problems.append(f"<use href={a.get('href')!r}>")
    assert not problems, f"{path.name}: dead references: {problems}"


@pages
def test_external_scripts_are_pinned(path):
    for a in parse(path).all("script"):
        src = a.get("src", "")
        if src.startswith(("http://", "https://", "//")):
            assert re.search(r"@\d+\.\d+\.\d+", src), f"{path.name}: unpinned script {src}"


@pages
def test_tags_are_balanced(path):
    page = parse(path)
    assert not page.errors, f"{path.name}: mismatched tags:\n  " + "\n  ".join(page.errors)
    assert not page.stack, f"{path.name}: unclosed: " + ", ".join(f"<{t}> line {n}" for t, n in page.stack)


# ------------------------------------------------------------- copy rules

def _copy(page: Page) -> list[str]:
    chunks = [page.title] + page.text
    for key in TEXT_META:
        chunks.append(page.meta(key) or "")
    for _, a in page.tags:
        chunks.extend(a[k] for k in ("aria-label", "alt", "title") if k in a)
    return chunks


@pages
def test_copy_has_no_hyphens_or_dashes(path):
    bad = sorted({c.strip() for c in _copy(parse(path)) if DASHES.search(c.replace("DQ-Stack", ""))})
    assert not bad, f"{path.name}: hyphens or dashes in copy: {bad}"


@pages
def test_copy_avoids_banned_filler(path):
    found = sorted({m.group(0) for c in _copy(parse(path)) for m in BANNED_FILLER.finditer(c)})
    assert not found, f"{path.name}: banned filler words: {found}"


def test_no_banned_typeface_in_any_site_file():
    files = [p for p in SITE.rglob("*") if p.suffix in (".css", ".html", ".js", ".svg")]
    assert files
    offenders = []
    for f in files:
        for m in BANNED_FONTS.finditer(f.read_text(encoding="utf-8")):
            offenders.append(f"{f.relative_to(SITE)}: {m.group(0)}")
    assert not offenders, "banned typefaces: " + ", ".join(offenders)


def test_fonts_load_with_swap_and_use_permitted_families():
    for path in PAGES:
        fonts = [h for h in parse(path).hrefs() if "fonts.googleapis.com/css" in h]
        assert fonts, f"{path.name}: Geist is not loaded"
        for href in fonts:
            assert "display=swap" in href, f"{path.name}: fonts must load with display=swap"
            families = re.findall(r"family=([^:&]+)", href)
            assert set(families) <= {"Geist", "Geist+Mono", "Manrope", "Poppins"}, families


# ------------------------------------------------------------ page specific

@pytest.mark.parametrize("name", ["privacy.html", "terms.html"])
def test_legal_pages_carry_the_lawyer_review_note(name):
    text = (SITE / name).read_text(encoding="utf-8").lstrip("﻿").lstrip()
    assert text.startswith(LEGAL_COMMENT), f"{name} must open with the draft notice comment"


def test_404_links_home():
    assert "/" in parse(SITE / "404.html").hrefs()


class Faq(html.parser.HTMLParser):
    """Question and answer text from <details class="faq-item"> blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[list[str]] = []
        self._part: int | None = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "details" and "faq-item" in (a.get("class") or ""):
            self.items.append(["", ""])
        elif self.items and tag == "summary":
            self._part = 0
        elif self.items and tag == "div" and "faq-body" in (a.get("class") or ""):
            self._part, self._depth = 1, 1
        elif self._part == 1 and tag == "div":
            self._depth += 1

    def handle_endtag(self, tag):
        if tag == "summary":
            self._part = None
        elif tag == "div" and self._part == 1:
            self._depth -= 1
            if self._depth == 0:
                self._part = None

    def handle_data(self, data):
        if self._part is not None:
            self.items[-1][self._part] += data


def _norm(s: str) -> str:
    return " ".join(s.split())


def test_faq_schema_matches_the_visible_questions():
    index = SITE / "index.html"
    faq = Faq()
    faq.feed(index.read_text(encoding="utf-8"))
    visible = [(_norm(q), _norm(a)) for q, a in faq.items]
    assert 6 <= len(visible) <= 12, f"the FAQ should hold six to twelve questions, has {len(visible)}"
    blocks = [json.loads(b) for b in parse(index).json_ld]
    schema = next(b for b in blocks if b.get("@type") == "FAQPage")
    declared = [(_norm(q["name"]), _norm(q["acceptedAnswer"]["text"])) for q in schema["mainEntity"]]
    assert declared == visible, "the FAQPage schema and the visible FAQ have drifted apart"
