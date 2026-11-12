"""The store app's Hindi (webapp/js/i18n.js, webapp/js/i18n-hi.js).

Static checks, no browser needed:

* every English string the staff screens can show has a Hindi entry. The staff
  screens are the sign-in pages and the frame (start.js), the shared helpers
  (core.js), Home and Alerts (screens-a.js), Today and Stock (screens-b.js), and
  Count, Receive, Transfers, Wastage, Checkout, Bills and returns and My shift
  (screens-c.js). Strings are found by a small JavaScript tokenizer: every argument
  of a translation call (tt, ttn, DQ.t, ...) and every literal that reads as English
  prose. Literals that are not shown to people are listed in NOT_SHOWN;
* every server message the dictionary translates still exists in dqstack/app, and
  every other key is used by the app (or is a word the screens build from a code);
* no Hindi entry is empty, keeps its English, or loses or gains a {placeholder};
* Hindi uses Latin digits only (Indian formats, no Devanagari digits).
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "webapp" / "js"
APP = ROOT / "dqstack" / "app"

# ---------------------------------------------------------------- the dictionary


def _dictionary() -> dict[str, str]:
    """Read DQ.i18n.add("hi", {...}) out of i18n-hi.js with node when available, else
    by parsing the object literal (it is plain JSON-like "key": "value" pairs)."""
    src = (JS / "i18n-hi.js").read_text(encoding="utf-8")
    if shutil.which("node"):
        script = ("global.window={DQ:{}};var DQ=window.DQ;var out={};DQ.i18n={add:function(c,d){"
                  "if(c==='hi')Object.assign(out,d);}};require(process.argv[1]);"
                  "process.stdout.write(JSON.stringify(out));")
        r = subprocess.run(["node", "-e", script, str(JS / "i18n-hi.js")], capture_output=True,
                           text=True, encoding="utf-8", check=True)
        return json.loads(r.stdout)
    out = {}
    for k, v in re.findall(r'^\s*"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)",?\s*$', src, re.M):
        out[json.loads(f'"{k}"')] = json.loads(f'"{v}"')
    return out


HI = _dictionary()

# ---------------------------------------------------------------- a JS tokenizer

_REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^")
_KEYWORDS_BEFORE_REGEX = {"return", "typeof", "case", "do", "else", "in", "of", "void"}
TRANSLATE_CALLS = {"tt", "ttn", "t", "tx", "DQ.t", "DQ.tn", "DQ.tx"}


def js_strings(src: str):
    """Yield (text, call, role) for every string literal. `call` is the name of the
    innermost function call the literal is an argument of; `role` is "key" for an
    object key, "compared" for the operand of ===, == or !==, else "value"."""
    i, n = 0, len(src)
    stack: list[str] = []          # callee per open bracket
    prev = ""                      # previous significant token
    while i < n:
        c = src[i]
        if c in " \t\r\n":
            i += 1
            continue
        if src.startswith("//", i):
            i = src.find("\n", i)
            i = n if i < 0 else i
            continue
        if src.startswith("/*", i):
            i = src.find("*/", i + 2) + 2
            continue
        if c in "\"'`":
            j, buf = i + 1, []
            while j < n and src[j] != c:
                if src[j] == "\\":
                    buf.append(src[j:j + 2])
                    j += 2
                    continue
                buf.append(src[j])
                j += 1
            raw = "".join(buf)
            try:
                text = json.loads('"' + raw.replace('"', '\\"').replace("\\'", "'") + '"')
            except json.JSONDecodeError:
                text = raw
            k = j + 1
            while k < n and src[k] in " \t\r\n":
                k += 1
            call = stack[-1] if stack else None
            before = src[:i].rstrip()
            if before.endswith(("==", "!=")) or (k < n and src[k] == "=" and src[k:k + 2] in ("==", "!=")):
                role = "compared"
            elif k < n and src[k] == ":" and before.endswith(("{", ",")):
                role = "key"
            elif k < n and src[k:k + 3] in ("===", "!=="):
                role = "compared"
            else:
                role = "value"
            yield text, call, role
            i = j + 1
            prev = "str"
            continue
        if c == "/" and (prev == "" or prev in _REGEX_BEFORE or prev in _KEYWORDS_BEFORE_REGEX):
            j, in_class = i + 1, False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                elif ch == "\n":
                    break
                j += 1
            i = j + 1
            while i < n and src[i].isalpha():
                i += 1
            prev = "regex"
            continue
        m = re.match(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", src[i:])
        if m:
            prev = m.group(0)
            i += len(prev)
            continue
        m = re.match(r"\d[\d.eE_xXa-fA-F]*", src[i:])
        if m:
            prev = "num"
            i += len(m.group(0))
            continue
        if c == "(":
            stack.append(prev if re.match(r"[A-Za-z_$]", prev or "") and prev not in
                         ("if", "while", "for", "switch", "function", "return", "catch") else None)
        elif c in "[{":
            stack.append(None)
        elif c in ")]}" and stack:
            stack.pop()
        prev = c
        i += 1


def _region(path: Path, *spans: tuple[str | None, str | None]) -> str:
    src = path.read_text(encoding="utf-8")
    parts = []
    for start, end in spans:
        a = src.index(start) if start else 0
        b = src.index(end, a) if end else len(src)
        parts.append(src[a:b])
    return "\n".join(parts)


SEP = "  /* ================================================================ "


def staff_sources() -> dict[str, str]:
    return {
        "core.js": _region(JS / "core.js", (None, None)),
        "start.js": _region(JS / "start.js", (None, None)),
        "screens-a.js": _region(JS / "screens-a.js", (None, "  var DAYS ="),
                                ("  /* Forecast accuracy in a shop owner's words. */", SEP + "Setup checklist */"),
                                (SEP + "Alerts */", SEP + "Activity log */")),
        "screens-b.js": _region(JS / "screens-b.js", (None, SEP + "Product */")),
        "screens-c.js": _region(JS / "screens-c.js", (None, SEP + "DAY CLOSE */"),
                                ("  /* ---------------------------------------------------------------- my shift", None)),
    }


# Literals in the staff code that look like English but are never shown as text.
NOT_SHOWN = {
    "Accept", "Content-Type", "X-DQ-Device", "X-CSRF-Token", "Content-Disposition",
    "Enter", "Escape", "ArrowDown", "ArrowUp", "INPUT", "TEXTAREA", "SELECT", "Node",
    "BarcodeDetector", "DQ-Stack", "Asia/Kolkata", "Geist",
    # Month names: Hindi takes its own from DQ.i18n.months(), not from the dictionary.
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    "Cr",                                  # rupees in crore: the abbreviation is kept
    # Offline-queue labels, kept in English on the device and translated when shown
    # (through the templates in QUEUE_LABELS below).
    "Count of", "Hold bill", "Void bill", "Transfer to", "Delivery",
}
PROSE = re.compile(r"^[A-Z][a-z]|^[A-Z] |^\{\w+\}")


def is_prose(text: str) -> bool:
    if not re.search(r"[A-Za-z]", text) or text in NOT_SHOWN:
        return False
    if re.fullmatch(r"[A-Z0-9_:./-]+", text):          # SKU:, UPI, GET, codes
        return False
    return bool(PROSE.match(text.strip()))


def shown_strings() -> dict[str, set[str]]:
    """English the staff screens can show, by file."""
    out: dict[str, set[str]] = {}
    for name, src in staff_sources().items():
        found = set()
        for text, call, role in js_strings(src):
            text = text.strip()
            if not text or text in NOT_SHOWN or role != "value":
                continue
            if call in TRANSLATE_CALLS and re.search(r"[A-Za-z]", text):
                found.add(text)
            elif is_prose(text):
                found.add(text)
        out[name] = found
    return out


# ---------------------------------------------------------------- server messages


def _py_skeletons() -> set[str]:
    """Every string the server code contains, with each f-string expression replaced by
    a marker, so "Bill {number} was voided." matches f"Bill {number} was voided."."""
    out = set()
    for f in APP.rglob("*.py"):
        if "migrations" in f.parts:
            continue
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.add(node.value)
            elif isinstance(node, ast.JoinedStr):
                out.add("".join(v.value if isinstance(v, ast.Constant) else "\x00"
                                for v in node.values))
    return out


def _skeleton(key: str) -> str:
    return re.sub(r"\{\w+\}", "\x00", key)


# Words the screens build from codes (cap(status), cap(reason)) rather than write out.
FROM_CODES = {
    "Draft", "Approved", "Sent", "Partly received", "Received", "Cancelled", "Closed",
    "Damaged", "Wrong item", "Expired", "Short dated", "Spoiled", "Theft found", "Other",
    "Cash", "Card", "Exchange", "Waste", "Damage", "Expiry", "In transit",
    "partly received", "received", "sent", "approved", "draft", "cancelled",
    "Info", "Risk", "Attention",
}

# The offline queue's labels (see NOT_SHOWN), shown through DQ.tx in "Not saved: ...".
QUEUE_LABELS = {
    "Count of {name}", "Hold bill {number}", "Void bill {number}", "Transfer to {store}",
    "Write off {name}", "Delivery for {number}", "Delivery, invoice {invoice}",
    "Delivery for {number}, invoice {invoice}",
}

# Server sentences whose plural is chosen inside the f-string, so each form is its own key.
SERVER_VARIANTS = {
    "This account is locked after too many wrong passwords. Try again in {n} minutes, or ask an administrator to unlock it.",
    "This account is locked after too many wrong passwords. Try again in {n} minute, or ask an administrator to unlock it.",
    "That code is not right. {n} tries left.",
    "That code is not right. {n} try left.",
}

# ---------------------------------------------------------------- tests


def test_dictionary_loads_and_is_large_enough():
    assert len(HI) > 400, len(HI)


def test_no_key_is_written_twice():
    src = (JS / "i18n-hi.js").read_text(encoding="utf-8")
    keys = re.findall(r'^\s*"((?:[^"\\]|\\.)*)"\s*:', src, re.M)
    twice = sorted({k for k in keys if keys.count(k) > 1})
    assert not twice, f"written twice (the second wins silently): {twice}"


def test_no_hindi_entry_is_empty_or_left_in_english():
    for k, v in HI.items():
        assert isinstance(v, str) and v.strip(), f"empty Hindi for {k!r}"
        assert re.search(r"[ऀ-ॿ]", v), f"no Devanagari in the Hindi for {k!r}: {v!r}"


def test_placeholders_match():
    for k, v in HI.items():
        assert sorted(re.findall(r"\{\w+\}", k)) == sorted(re.findall(r"\{\w+\}", v)), \
            f"placeholders differ: {k!r} -> {v!r}"


def test_digits_stay_latin():
    for k, v in HI.items():
        assert not re.search(r"[०-९]", v), f"Devanagari digits in {k!r}: {v!r}"


def test_every_staff_string_has_hindi():
    missing = {name: sorted(s - set(HI)) for name, s in shown_strings().items()}
    missing = {k: v for k, v in missing.items() if v}
    assert not missing, "English with no Hindi entry:\n" + "\n".join(
        f"  {name}: {s!r}" for name, lst in missing.items() for s in lst)


def test_every_key_is_used_or_harmless():
    js_literals = set()
    for f in JS.glob("*.js"):
        if f.name.startswith("i18n"):
            continue
        js_literals |= {t.strip() for t, _, _ in js_strings(f.read_text(encoding="utf-8"))}
    py = _py_skeletons()
    py_stripped = {s.strip() for s in py}
    unused = []
    for k in HI:
        if k in js_literals or k in FROM_CODES or k in QUEUE_LABELS or k in SERVER_VARIANTS or k in py_stripped:
            continue
        if "{" in k and _skeleton(k) in {_skeleton(s) for s in py_stripped}:
            continue
        unused.append(k)
    assert not unused, "Dictionary keys nothing uses (remove them, or add the screen text):\n" + \
        "\n".join(f"  {k!r}" for k in unused)


def test_server_messages_translate_by_template():
    """A server sentence with a product name in it is found through its template."""
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    script = r"""
    global.window = { DQ: {} }; global.document = { documentElement: {}, getElementById: function () { return {}; },
      createElement: function () { return {}; }, head: { appendChild: function () {} } };
    global.localStorage = { getItem: function () { return 'hi'; }, setItem: function () {} };
    require(process.argv[1]); require(process.argv[2]);
    var DQ = window.DQ; DQ.i18n.set('hi');
    var out = {
      exact: DQ.tx('Saved.'),
      tpl: DQ.tx('Sugar 1 kg is counted in whole units.'),
      t: DQ.t('Bill {number} held. Recall it from Held bills.', { number: 'S1-AB-00012' }),
      plural: DQ.tn(3, '{n} item', '{n} items'),
      untouched: DQ.tx('Some sentence nobody translated.'),
      spaced: DQ.tx(' · '),
    };
    process.stdout.write(JSON.stringify(out));
    """
    r = subprocess.run(["node", "-e", script, str(JS / "i18n.js"), str(JS / "i18n-hi.js")],
                       capture_output=True, text=True, encoding="utf-8", check=True)
    out = json.loads(r.stdout)
    assert out["exact"] == HI["Saved."]
    assert out["tpl"].startswith("Sugar 1 kg ") and out["tpl"] != "Sugar 1 kg is counted in whole units."
    assert "S1-AB-00012" in out["t"] and re.search(r"[ऀ-ॿ]", out["t"])
    assert out["plural"].startswith("3 ")
    assert out["untouched"] == "Some sentence nobody translated."
    assert out["spaced"] == " · "
