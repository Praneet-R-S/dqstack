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
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX = ROOT / "ui" / "index.html"

MODULES = ["ui/js/app.js", "ui/js/review.js", "ui/js/catalog.js", "ui/js/plain.js",
           "ui/js/duck.js", "ui/js/grid.js", "ui/js/chart.js", "ui/js/views.js",
           "ui/js/util.js", "ui/js/truth.js", "ui/js/curve.js", "ui/js/stock.js",
           "ui/js/orders.js", "ui/js/charts.js", "ui/js/overview.js"]

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
    for src_file in ("js/plain.js", "js/truth.js", "js/curve.js", "js/stock.js",
                     "js/util.js", "js/review.js", "js/orders.js", "js/catalog.js",
                     "js/app.js"):
        assert f'src="{src_file}' in src, f"index.html no longer loads {src_file}"
    for view in ("overview", "today", "orders", "products", "charts", "workspace", "evidence",
                 "decisions", "pipeline"):
        assert f'id="view-{view}"' in src, f"index.html lost view-{view}"


@pytest.mark.parametrize("rel", MODULES)
def test_ids_referenced_by_js_all_exist(rel):
    """A single stale `$("id")` throws and silently aborts everything after it.

    This checked app.js alone until orders.js shipped with a `$("orUncounted")` left
    over from an earlier draft of its header. The throw happened inside the render,
    so the whole order list came up empty with nothing on screen to say why — and the
    console named a line in a file the developer was not looking at.

    The failure mode belongs to every module that touches the DOM, not to app.js, so
    the check does too.
    """
    import re

    src = INDEX.read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([A-Za-z0-9_]+)"', src))
    # Ids the modules render themselves count too: catalog.js writes `id="catQty"`
    # into a template literal and then looks it up, which is legitimate. Only an id
    # that exists in neither the document nor any module's output is a stale
    # reference. Interpolated ids (`id="ed-${...}"`) are skipped by the pattern,
    # which is correct -- they are never looked up by a literal string either.
    for mod in MODULES:
        ids |= set(re.findall(r'id="([A-Za-z0-9_]+)"',
                              (ROOT / mod).read_text(encoding="utf-8")))
    js = (ROOT / rel).read_text(encoding="utf-8")
    refs = set(re.findall(r'\$\("([A-Za-z0-9_]+)"\)', js))
    refs |= set(re.findall(r'getElementById\("([A-Za-z0-9_]+)"\)', js))
    missing = sorted(r for r in refs - ids)
    assert not missing, f"{rel} references ids that do not exist: {missing}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
@pytest.mark.parametrize("rel", MODULES)
def test_every_module_parses(rel):
    """A syntax error in any module takes the whole application down silently.

    These files are plain scripts, not modules, so one bad token stops that file
    executing and every later file that dereferences its global throws in turn. What
    reaches the browser is a blank shell and a console the developer may not be
    looking at. It happened here: a `const shown` collided with an existing binding
    in the same function, and the first symptom was an unrelated "Review is not
    defined" from app.js.

    `node --check` catches the whole class in milliseconds.
    """
    proc = subprocess.run([shutil.which("node"), "--check", str(ROOT / rel)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"{rel} does not parse:\n{proc.stderr}"


@pytest.mark.parametrize("rel", MODULES)
def test_no_stray_control_characters(rel):
    """Heredoc round-trips have twice written a literal backspace or a raw BOM into
    a source file, where it is invisible in a diff and breaks a regex or a parse."""
    text = (ROOT / rel).read_text(encoding="utf-8")
    bad = {hex(ord(c)) for c in text if ord(c) < 32 and c not in "\n\r\t"}
    assert not bad, f"{rel} contains control characters: {sorted(bad)}"
    assert "�" not in text, f"{rel} contains a replacement character"
