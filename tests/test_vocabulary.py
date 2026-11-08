"""No method vocabulary on a planner-facing surface.

The interface argues that a forecast should be actionable without understanding the
mechanism that produced it. That is a claim about wording, and wording drifts: one
hurried sentence reintroduces "quantile" or "fractile" into the surface a buyer reads
every morning, and nothing catches it because it still renders fine.

So the constraint is enforced here rather than remembered. This is acceptance test 3
from the external design review, made executable.

What counts as planner-facing:

    ui/js/plain.js      every word a non-specialist reads
    ui/js/review.js     Today, the decision queue
    ui/js/catalog.js    Products
    ui/js/truth.js      the excluded-day layer every chart reads

What is deliberately exempt, because the design permits the real term at the deepest
rung of disclosure:

    `term:` fields          the technical name, offered beside the plain phrase
    title="..." attributes  hover disclosure on a figure
    comments                addressed to developers, never rendered
    identifiers             `fractile()`, `maxQuantile`, `s.adi` -- code, not copy
    object keys             `nhits:`, `adida:` -- the pipeline's own method names,
                            keyed precisely so they can map to a plain label
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SURFACES = ("ui/js/plain.js", "ui/js/review.js", "ui/js/catalog.js",
            "ui/js/truth.js", "ui/js/stock.js", "ui/js/orders.js",
            "ui/js/charts.js", "ui/js/overview.js")

# Whole words only, so "SBA" does not fire inside "rvProgressBar" and "ADI" does not
# fire inside "predictable".
BANNED = [
    "MinT", "conformal", "quantile", "quantiles", "WSPL", "WRMSSE", "MASE",
    "Croston", "Tweedie", "fractile", "NHITS", "ADIDA", "TSB", "pinball",
    "Schaefer", "Strimmer", "PELT", "CV-squared", "Wilcoxon", "heteroscedastic",
    "censoring", "featurisation", "reconciliation", "LightGBM", "SHAP",
]


def strip_non_prose(src: str) -> str:
    """Remove everything that is not rendered to a planner."""
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)          # block comments
    src = re.sub(r"^\s*//.*$", " ", src, flags=re.M)          # whole-line comments
    # Trailing comments too. Requires whitespace before the slashes, so a URL
    # scheme ("https://") is never mistaken for one.
    src = re.sub(r"\s+//[^\n]*$", " ", src, flags=re.M)
    src = re.sub(r'title="[^"]*"', " ", src)                  # hover disclosure
    src = re.sub(r"title=\"\$\{[^}]*\}\"", " ", src)
    src = re.sub(r'term:\s*"[^"]*"', " ", src)                # the real name, offered
    # Identifiers: a banned word touching a dot, a paren, or camelCase is code.
    src = re.sub(r"\b\w+\.\w+\b", " ", src)
    src = re.sub(r"\b\w*[a-z]\w*\(", " ", src)
    # Object keys are identifiers too. The model lookup is keyed by the pipeline's own
    # method names -- `nhits:`, `adida:`, `tsb:` -- whose whole purpose is to map to a
    # plain label, so the key is code in exactly the way `fractile()` is. Anchored to
    # a line start, a brace or a comma, which is where a JS key sits and where prose
    # never does, so "Warning: shelf empty" inside a string is still checked.
    src = re.sub(r"(^|[{,]\s*)([A-Za-z_$][\w$]*)\s*:", r"\1 ", src, flags=re.M)
    # Quoted keys too. The policy lookup is keyed by the exact strings the pipeline
    # writes into the metrics -- "Croston/SBA only", "no reconciliation" -- because
    # matching on them is the only way to attach a plain label to each. Same anchor as
    # above, so a quoted string anywhere other than key position is still checked, and
    # the label and note values beside these keys are still checked.
    src = re.sub(r'(^|[{,]\s*)"[^"\n]*"\s*:', r"\1 ", src, flags=re.M)
    return src


def leaks(path: pathlib.Path) -> list[tuple[int, str, str]]:
    src = path.read_text(encoding="utf-8")
    prose = strip_non_prose(src)
    found = []
    for word in BANNED:
        for m in re.finditer(rf"\b{re.escape(word)}\b", prose, re.I):
            line = prose[: m.start()].count("\n") + 1
            found.append((line, word, prose.splitlines()[line - 1].strip()[:120]))
    return found


@pytest.mark.parametrize("rel", SURFACES)
def test_no_method_vocabulary_on_planner_surfaces(rel):
    path = ROOT / rel
    assert path.exists(), rel
    found = leaks(path)
    if found:
        detail = "\n".join(f"  line {ln}: {word!r} in: {ctx}" for ln, word, ctx in found)
        pytest.fail(
            f"{rel} exposes method vocabulary to a planner:\n{detail}\n\n"
            "Put the plain phrase in the copy and the technical term in a `term:` "
            "field or a title attribute, which are the permitted disclosure routes."
        )


def test_the_check_can_actually_fail():
    """A guard that would otherwise pass silently if strip_non_prose over-stripped.

    Without this, tightening the stripper until nothing matches would look like
    success. A deliberately planted string has to be caught.
    """
    planted = 'const msg = "We reconciled the quantile forecast with MinT.";'
    assert len(leaks_in_text(planted)) >= 2


def leaks_in_text(text: str) -> list[str]:
    prose = strip_non_prose(text)
    return [w for w in BANNED if re.search(rf"\b{re.escape(w)}\b", prose, re.I)]


def test_plain_language_vocabulary_is_actually_used():
    """The positive half: the plain phrases the design promises must be present."""
    src = (ROOT / "ui" / "js" / "plain.js").read_text(encoding="utf-8")
    for phrase in ("Sells steadily", "Sells in bursts", "Likely to run out",
                   "May have been out of stock", "Hard to predict",
                   "Too new to judge", "Winding down",
                   # The excluded-day layer and the blend, whose whole job is to say
                   # these things without the method names behind them.
                   "Not stocked", "Shelf probably empty",
                   "This product's own sales pattern",
                   "The curve new products follow",
                   "Built for occasional sellers"):
        assert phrase in src, f"plain.js lost the phrase {phrase!r}"


def test_every_blended_method_has_a_plain_label():
    """A method with no entry in MODELS renders its raw key to a planner.

    The ensemble is configurable, so a method added to the pipeline arrives in the
    exported weights without anyone touching the interface. The fallback prints
    `croston_sba` with the underscore swapped for a space, which is both ugly and
    exactly the vocabulary this file exists to keep off the screen.
    """
    import json

    bundle = json.loads((ROOT / "ui" / "data" / "bundle.json").read_text(encoding="utf-8"))
    used = {k for s in bundle["series"] for k, w in s["weights"].items() if w > 0}
    src = (ROOT / "ui" / "js" / "plain.js").read_text(encoding="utf-8")
    missing = sorted(k for k in used if f"{k}:" not in src)
    assert not missing, (
        f"these methods carry weight but have no plain label in plain.js: {missing}"
    )

def test_plain_js_is_the_only_table_of_user_facing_labels():
    """One vocabulary, or it drifts -- and it had.

    util.js carried a second label table, REASON_LABEL, whose values were
    "service risk", "stale data", "censored history", "wide interval",
    "moved vs prior", "cold start" and "terminal decline". plain.js has had
    "Running out", "Pattern changed", "Shelf looked empty", "Hard to predict",
    "Moved a lot", "Too new" and "Winding down" for the same seven keys since it was
    written. Two tables meant two vocabularies, and the spreadsheet was showing the
    wrong one to anyone who opened it -- including, through the grid's accessibility
    mirror, to screen-reader users who had no visual context to compensate.

    plain.js is the declared single source of user-facing wording. This asserts no
    module reintroduces a rival.
    """
    banned_tables = ["REASON_LABEL", "CLASS_LABEL", "REASON_WORD"]
    offenders = []
    for rel in ("ui/js/util.js", "ui/js/app.js", "ui/js/grid.js", "ui/js/chart.js",
                "ui/js/orders.js", "ui/js/stock.js"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        for name in banned_tables:
            if f"const {name}" in src or f"{name} =" in src:
                offenders.append(f"{rel} defines {name}")
    assert not offenders, (
        "a second table of user-facing labels has reappeared:\n  "
        + "\n  ".join(offenders)
        + "\nPut the wording in plain.js and read it from there."
    )


def test_the_worklist_reads_its_labels_from_plain_js():
    """The spreadsheet's filter chips printed the raw reason keys.

    They read `risk`, `stale`, `censored`, `wide`, `swing`, `cold-start`, `terminal`
    to the user -- the pipeline's own identifiers, on a surface a planner opens to
    edit many products at once. The keys were always the same as plain.js's, so the
    plain label was one lookup away and simply never asked for.
    """
    src = (ROOT / "ui" / "js" / "app.js").read_text(encoding="utf-8")
    i = src.find("function renderFilters")
    assert i > 0, "renderFilters has been renamed; update this test"
    block = src[i:i + 1400]
    assert "Plain.short(" in block, (
        "the worklist chips no longer render through Plain.short(), so they are "
        "printing raw reason keys to the user again"
    )
