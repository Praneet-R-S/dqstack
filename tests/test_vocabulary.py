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
SURFACES = ("ui/js/plain.js", "ui/js/review.js", "ui/js/catalog.js", "ui/js/truth.js")

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
