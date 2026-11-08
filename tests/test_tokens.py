"""The design tokens must stay internally consistent.

tokens.css is now three layers: primitives (raw values), semantic roles that point
at them, and components that read only the roles. Two things can break silently:

1. The canvas renderers read colours through `--x-rgb` channel triples, which are
   literal numbers because a hex value cannot be split into channels in CSS. If a
   semantic colour is changed and its triple is not, a chart paints its band in one
   purple under a line in another, and nothing throws.

2. The grid and chart resolve a fixed set of token NAMES at paint time. Renaming one
   in a palette change is not a CSS edit; it is a blank chart in an unrelated file.

Both are checked here, in both themes.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOKENS = (ROOT / "ui" / "css" / "tokens.css").read_text(encoding="utf-8")

# Names the canvas renderers and SVG charts read by name. Losing any is a silent
# rendering failure, which is why they are pinned rather than discovered.
LOAD_BEARING = [
    "--bg-base", "--bg-surface", "--bg-raised", "--bg-inset", "--bg-overlay",
    "--border-subtle", "--border-strong", "--text-primary", "--text-secondary",
    "--text-tertiary", "--series-forecast", "--series-actual", "--series-override",
    "--series-prior", "--state-risk", "--state-excess", "--state-attention",
    "--state-ok", "--ui-accent", "--forecast-rgb", "--risk-rgb", "--excess-rgb",
    "--override-rgb", "--ui-accent-rgb", "--font-num", "--font-ui", "--row-h",
    "--cell-fs",
]

PAIRS = {"--forecast-rgb": "--series-forecast", "--risk-rgb": "--state-risk",
         "--excess-rgb": "--state-excess", "--override-rgb": "--series-override",
         "--ui-accent-rgb": "--ui-accent"}


def _block(selector: str) -> str:
    i = TOKENS.index(selector)
    depth, j = 0, TOKENS.index("{", i)
    for k in range(j, len(TOKENS)):
        if TOKENS[k] == "{":
            depth += 1
        elif TOKENS[k] == "}":
            depth -= 1
            if depth == 0:
                return TOKENS[j + 1:k]
    raise AssertionError(f"unterminated block {selector}")


def _decls(block: str) -> dict[str, str]:
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", block)}


DARK = _decls(_block(":root {"))
LIGHT = {**DARK, **_decls(_block(':root[data-theme="light"] {'))}


def _resolve(env: dict[str, str], value: str, depth: int = 0) -> str:
    m = re.fullmatch(r"var\((--[\w-]+)\)", value.strip())
    if not m:
        return value.strip()
    assert depth < 8, f"var() cycle at {value}"
    return _resolve(env, env[m.group(1)], depth + 1)


def _hex_to_triple(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def test_every_load_bearing_token_still_exists():
    missing = [n for n in LOAD_BEARING if n not in DARK]
    assert not missing, f"tokens the renderers read by name have gone: {missing}"


def test_channel_triples_match_their_colours_in_both_themes():
    problems = []
    for theme, env in (("dark", DARK), ("light", LIGHT)):
        for triple, colour in PAIRS.items():
            hexv = _resolve(env, env[colour])
            assert hexv.startswith("#"), f"{theme} {colour} does not resolve to a hex"
            want = _hex_to_triple(hexv)
            got = tuple(int(x) for x in env[triple].split())
            if got != want:
                problems.append(f"{theme}: {triple} is {got} but {colour} is {hexv} = {want}")
    assert not problems, "channel triples out of step:\n  " + "\n  ".join(problems)


def test_semantic_roles_reference_primitives_rather_than_raw_hex():
    """The point of the three layers: one primitive change re-themes every role
    that uses it, and a role with a pasted hex quietly opts out of that."""
    raw = [f"{n}: {v}" for n, v in DARK.items()
           if re.match(r"--(bg|text|border|series|state|ui)-", n)
           and not n.endswith("-rgb") and v.startswith("#")]
    assert not raw, "semantic roles holding raw hex:\n  " + "\n  ".join(raw)


def test_data_and_action_purples_stay_apart():
    """Two purples on purpose -- a violet for data and an orchid for actions. If
    they drift together the app loses the difference between "this is a forecast"
    and "click me", which it learned once already."""
    import colorsys

    for theme, env in (("dark", DARK), ("light", LIGHT)):
        hues = []
        for name in ("--series-forecast", "--ui-accent"):
            r, g, b = (c / 255 for c in _hex_to_triple(_resolve(env, env[name])))
            hues.append(colorsys.rgb_to_hls(r, g, b)[0] * 360)
        assert abs(hues[0] - hues[1]) >= 20, (
            f"{theme}: data and action hues are only {abs(hues[0] - hues[1]):.1f} "
            "degrees apart")
