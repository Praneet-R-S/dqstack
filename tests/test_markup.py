"""index.html must be structurally sound.

This exists because it was not, twice, and neither failure was visible. A footer
replacement consumed the `</div>` that closed it; browsers auto-closed the element
and the layout still rendered, so nothing complained. The next edit's search for
"the next `</div>`" then landed far later in the document and swallowed the sidebar
close, the whole chrome opening, and the cycle ribbon with it.

An unbalanced tag in this file is not a cosmetic problem. Every view is a grid child
pinned to a row, and the grid has collapsed to zero height three times in this
project for related reasons. A tag-balance assertion is two lines of value for a
class of bug that costs an hour each time.
"""

from __future__ import annotations

import html.parser
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX = ROOT / "ui" / "index.html"

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "source", "track", "wbr"}


class Balance(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, int]] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        pass                                    # self-closing, nothing to track

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        line = self.getpos()[0]
        if not self.stack:
            self.errors.append(f"stray </{tag}> at line {line}")
            return
        if self.stack[-1][0] != tag:
            open_tag, open_line = self.stack[-1]
            self.errors.append(
                f"</{tag}> at line {line} closes <{open_tag}> opened at line {open_line}")
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag:
                    del self.stack[i:]
                    break
        else:
            self.stack.pop()


def parse() -> Balance:
    b = Balance()
    b.feed(INDEX.read_text(encoding="utf-8"))
    return b


def test_every_tag_is_balanced():
    b = parse()
    assert not b.errors, "index.html has mismatched tags:\n  " + "\n  ".join(b.errors)
    assert not b.stack, (
        "index.html leaves elements unclosed:\n  "
        + "\n  ".join(f"<{t}> opened at line {ln}" for t, ln in b.stack)
    )


def test_the_landmarks_the_shell_depends_on_are_present():
    """Named because losing any of these breaks the layout silently.

    `.sidebar` and `.chrome` are the two grid children of #app; the six views are
    pinned to row 2 column 2. A missing landmark does not throw — it produces a
    region with no height, which is the failure mode already recorded three times.
    """
    src = INDEX.read_text(encoding="utf-8")
    for needle in ('class="sidebar"', 'class="chrome"', 'class="sb-cycle"',
                   'class="sb-nav"', 'class="sb-foot"', 'id="app"',
                   'id="sbQueue"', 'id="sbSession"'):
        assert needle in src, f"index.html lost {needle}"
    # Every module the renderers dereference at paint time. A dropped tag here is a
    # ReferenceError inside a template literal, which renders as a blank card.
    for src_file in ("js/plain.js", "js/truth.js", "js/curve.js", "js/util.js",
                     "js/review.js", "js/catalog.js", "js/app.js"):
        assert f'src="{src_file}' in src, f"index.html no longer loads {src_file}"
    for view in ("today", "products", "workspace", "evidence", "decisions", "pipeline"):
        assert f'id="view-{view}"' in src, f"index.html lost view-{view}"


def test_ids_referenced_by_app_js_all_exist():
    """A single stale `$("id")` in boot() throws and silently aborts every binding
    after it, which presents as several unrelated features being broken at once."""
    import re

    src = INDEX.read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([A-Za-z0-9_]+)"', src))
    # Template content counts: it is cloned into the document.
    js = (ROOT / "ui" / "js" / "app.js").read_text(encoding="utf-8")
    refs = set(re.findall(r'\$\("([A-Za-z0-9_]+)"\)', js))
    refs |= set(re.findall(r'getElementById\("([A-Za-z0-9_]+)"\)', js))
    missing = sorted(r for r in refs - ids)
    assert not missing, f"app.js references ids that do not exist: {missing}"


@pytest.mark.parametrize("rel", ["ui/js/app.js", "ui/js/review.js", "ui/js/catalog.js",
                                 "ui/js/plain.js", "ui/js/duck.js", "ui/js/grid.js",
                                 "ui/js/chart.js", "ui/js/views.js", "ui/js/util.js",
                                 "ui/js/truth.js", "ui/js/curve.js"])
def test_no_stray_control_characters(rel):
    """Heredoc round-trips have twice written a literal backspace or a raw BOM into
    a source file, where it is invisible in a diff and breaks a regex or a parse."""
    text = (ROOT / rel).read_text(encoding="utf-8")
    bad = {hex(ord(c)) for c in text if ord(c) < 32 and c not in "\n\r\t"}
    assert not bad, f"{rel} contains control characters: {sorted(bad)}"
    assert "�" not in text, f"{rel} contains a replacement character"
