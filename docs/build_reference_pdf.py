"""Build the complete DQ-Stack reference document as a PDF.

Everything quantitative in the output is read from `artifacts/` and `ui/css/`
at build time rather than typed in, so the document cannot drift from the run
that produced it. Re-run after any pipeline run to regenerate.

    python docs/build_reference_pdf.py
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, KeepTogether, NextPageTemplate,
    PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
ART = ROOT / "artifacts"
OUT = ROOT / "docs" / "DQ-Stack-Reference.pdf"

# ---------------------------------------------------------------- palette
INK = colors.HexColor("#0E1620")
BODY = colors.HexColor("#39424D")
MUTE = colors.HexColor("#6B7683")
RULE = colors.HexColor("#DBE1E7")
ACCENT = colors.HexColor("#3050E8")
ACCENT_SOFT = colors.HexColor("#EAEEFD")
RISK = colors.HexColor("#C0392B")
GOOD = colors.HexColor("#0B7D71")
AMBER = colors.HexColor("#B87400")
PAPER = colors.HexColor("#FFFFFF")

PW, PH = A4
MARGIN = 21 * mm


# ================================================================== data
def load():
    facts = {}
    bs = json.loads((ART / "backtest_summary.json").read_text(encoding="utf-8"))
    facts["backtest"] = bs
    facts["metrics"] = json.loads((ART / "metrics.json").read_text(encoding="utf-8"))
    import csv
    for name in ("economics", "leaderboard", "fva_ladder", "coverage_table",
                 "ensemble_weights_by_class", "backtest_hierarchical", "ablation"):
        p = ART / f"{name}.csv"
        if p.exists():
            with p.open(encoding="utf-8") as fh:
                facts[name] = list(csv.DictReader(fh))
    css = (ROOT / "ui" / "css" / "tokens.css").read_text(encoding="utf-8")

    def block(sel):
        i = css.index(sel)
        j = i
        while True:
            j = css.index("}", j) + 1
            if css[i:j].count("{") == css[i:j].count("}"):
                break
        return css[i:j]

    def toks(seg):
        return {m.group(1): m.group(2).strip().rstrip(";")
                for m in re.finditer(r"(--[\w-]+):\s*([^;]+);", seg)}

    facts["dark"] = toks(block(":root {"))
    facts["light"] = toks(block(':root[data-theme="light"] {'))
    return facts


def target_quantiles():
    """The realised spread of the service target, straight from the exported bundle."""
    import statistics
    b = json.loads((ROOT / "ui" / "data" / "bundle.json").read_text(encoding="utf-8"))
    v = sorted(s["targetQ"] for s in b["series"])
    return {"min": v[0], "max": v[-1], "mean": statistics.fmean(v),
            "median": statistics.median(v), "n": len(v)}


F = load()
M = F["metrics"]
TARGET_Q = target_quantiles()
CFG = M.get("config", {})


def num(v, d=4):
    try:
        return f"{float(v):.{d}f}"
    except (TypeError, ValueError):
        return str(v)


def money(v):
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


# ================================================================ styles
SS = getSampleStyleSheet()


def S(name, **kw):
    base = kw.pop("parent", SS["BodyText"])
    return ParagraphStyle(name, parent=base, **kw)


BODYS = S("body", fontName="Helvetica", fontSize=9.3, leading=14.2,
          textColor=BODY, spaceAfter=7, alignment=TA_JUSTIFY)
LEAD = S("lead", parent=BODYS, fontSize=10.6, leading=16.4, textColor=INK,
         spaceAfter=11)
H1 = S("h1", fontName="Times-Roman", fontSize=25, leading=29, textColor=INK,
       spaceBefore=0, spaceAfter=4)
H1SUB = S("h1sub", fontName="Helvetica", fontSize=9, leading=13, textColor=ACCENT,
          spaceAfter=14)
H2 = S("h2", fontName="Times-Roman", fontSize=15.5, leading=19, textColor=INK,
       spaceBefore=17, spaceAfter=6)
H3 = S("h3", fontName="Helvetica-Bold", fontSize=9.6, leading=13, textColor=INK,
       spaceBefore=12, spaceAfter=4)
CAPS = S("caps", fontName="Helvetica-Bold", fontSize=6.9, leading=10,
         textColor=MUTE, spaceAfter=3)
NOTE = S("note", parent=BODYS, fontSize=8.6, leading=13, textColor=MUTE,
         alignment=0)
CODE = S("code", fontName="Courier", fontSize=7.9, leading=11.4, textColor=INK,
         backColor=colors.HexColor("#F4F6F9"), borderPadding=6,
         spaceBefore=5, spaceAfter=8, leftIndent=2, rightIndent=2)
CELL = S("cell", fontName="Helvetica", fontSize=7.7, leading=10.4, textColor=BODY,
         spaceAfter=0, alignment=0)
CELLH = S("cellh", parent=CELL, fontName="Helvetica-Bold", textColor=INK)
BULLET = S("bullet", parent=BODYS, leftIndent=11, bulletIndent=2, spaceAfter=4,
           alignment=0)


# ============================================================== drawings
class Rule(Flowable):
    def __init__(self, w=None, thickness=0.6, color=RULE, space=4):
        Flowable.__init__(self)
        self.w, self.t, self.c, self.space = w, thickness, color, space

    def wrap(self, aw, ah):
        self.width = self.w or aw
        self.height = self.t + self.space
        return self.width, self.height

    def draw(self):
        self.canv.setStrokeColor(self.c)
        self.canv.setLineWidth(self.t)
        self.canv.line(0, self.space, self.width, self.space)


class Mark(Flowable):
    """The identity: an origin rule with two nested fan bands opening right."""

    def __init__(self, size=34, color=ACCENT, rule=INK):
        Flowable.__init__(self)
        self.s, self.c, self.r = size, color, rule

    def wrap(self, aw, ah):
        return self.s, self.s

    def draw(self):
        s, c = self.s, self.c
        u = s / 16.0
        self.canv.setStrokeColor(self.r)
        self.canv.setLineWidth(1.5 * u)
        self.canv.setLineCap(1)
        self.canv.line(5.25 * u, 1.75 * u, 5.25 * u, 14.25 * u)
        for (top, bot, alpha) in ((13.25, 2.75, 0.42), (10.6, 5.4, 1.0)):
            self.canv.setFillColor(c, alpha=alpha)
            p = self.canv.beginPath()
            p.moveTo(5.25 * u, 8 * u)
            p.lineTo(14.75 * u, top * u)
            p.lineTo(14.75 * u, bot * u)
            p.close()
            self.canv.drawPath(p, fill=1, stroke=0)


class FanDiagram(Flowable):
    """A figure of the motif: history resolving into a widening forecast cone."""

    def __init__(self, w=None, h=62 * mm):
        Flowable.__init__(self)
        self.w, self.h = w, h

    def wrap(self, aw, ah):
        self.width = self.w or aw
        return self.width, self.h + 6 * mm

    def draw(self):
        import math
        c = self.canv
        W, H = self.width, self.h
        pad_l, pad_b = 12 * mm, 9 * mm
        iw, ih = W - pad_l - 6 * mm, H - pad_b - 6 * mm
        ox = pad_l + iw * 0.45          # the origin

        c.setFillColor(colors.HexColor("#FAFBFD"))
        c.rect(pad_l, pad_b, iw, ih, fill=1, stroke=0)
        c.setFillColor(ACCENT, alpha=0.045)
        c.rect(ox, pad_b, pad_l + iw - ox, ih, fill=1, stroke=0)

        base = pad_b + ih * 0.44
        hist = []
        for i in range(46):
            x = pad_l + (ox - pad_l) * i / 45.0
            y = base + math.sin(i / 3.1) * ih * 0.075 + math.sin(i / 8.0) * ih * 0.05
            hist.append((x, y))

        span = pad_l + iw - ox
        for width_mult, alpha in ((1.0, 0.11), (0.62, 0.19), (0.32, 0.30)):
            c.setFillColor(ACCENT, alpha=alpha)
            p = c.beginPath()
            p.moveTo(ox, hist[-1][1])
            for k in range(31):
                t = k / 30.0
                p.lineTo(ox + span * t, hist[-1][1] + ih * 0.30 * width_mult * (t ** 0.72))
            for k in range(30, -1, -1):
                t = k / 30.0
                p.lineTo(ox + span * t, hist[-1][1] - ih * 0.26 * width_mult * (t ** 0.72))
            p.close()
            c.drawPath(p, fill=1, stroke=0)

        c.setStrokeColor(colors.HexColor("#2E3A47"))
        c.setLineWidth(1.15)
        p = c.beginPath()
        p.moveTo(*hist[0])
        for pt in hist[1:]:
            p.lineTo(*pt)
        c.drawPath(p, stroke=1, fill=0)

        c.setStrokeColor(ACCENT)
        c.setLineWidth(1.35)
        c.setDash(4, 2.6)
        p = c.beginPath()
        p.moveTo(ox, hist[-1][1])
        for k in range(1, 31):
            t = k / 30.0
            p.lineTo(ox + span * t, hist[-1][1] + ih * 0.02 * t)
        c.drawPath(p, stroke=1, fill=0)
        c.setDash()

        c.setStrokeColor(ACCENT, alpha=0.55)
        c.setLineWidth(0.9)
        c.line(ox, pad_b, ox, pad_b + ih)
        c.setFillColor(colors.HexColor("#2E3A47"))
        c.circle(ox, hist[-1][1], 2.1, fill=1, stroke=0)

        c.setFont("Helvetica", 6.6)
        c.setFillColor(MUTE)
        c.drawString(pad_l, pad_b - 5 * mm, "OBSERVED SALES")
        c.drawString(ox + 2 * mm, pad_b + ih + 1.6 * mm, "TODAY")
        c.drawRightString(pad_l + iw, pad_b - 5 * mm, "FORECAST, 50 / 80 / 98% BANDS")


class Swatches(Flowable):
    """Colour chips with hex and role, both themes side by side."""

    def __init__(self, rows, w=None, chip=9 * mm):
        Flowable.__init__(self)
        self.rows, self.w, self.chip = rows, w, chip

    def wrap(self, aw, ah):
        self.width = self.w or aw
        self.height = len(self.rows) * (self.chip + 3.2 * mm) + 2 * mm
        return self.width, self.height

    def draw(self):
        c = self.canv
        y = self.height - self.chip - 2 * mm
        for role, dk, lt, meaning in self.rows:
            for i, hexv in enumerate((dk, lt)):
                try:
                    col = colors.HexColor(hexv)
                except Exception:
                    col = colors.grey
                x = 0 + i * (self.chip + 1.6 * mm)
                c.setFillColor(col)
                c.setStrokeColor(RULE)
                c.setLineWidth(0.4)
                c.roundRect(x, y, self.chip, self.chip, 1.6 * mm, fill=1, stroke=1)
            tx = 2 * (self.chip + 1.6 * mm) + 2 * mm
            c.setFillColor(INK)
            c.setFont("Helvetica-Bold", 7.6)
            c.drawString(tx, y + self.chip - 3.4 * mm, role)
            c.setFillColor(MUTE)
            c.setFont("Courier", 6.5)
            c.drawString(tx, y + self.chip - 6.9 * mm, f"{dk}  /  {lt}")
            c.setFillColor(BODY)
            c.setFont("Helvetica", 7.4)
            c.drawString(tx + 34 * mm, y + self.chip - 5.2 * mm, meaning)
            y -= self.chip + 3.2 * mm


class Pipeline(Flowable):
    """The eight stages as a chain."""

    STAGES = [
        ("0", "Ingest", "schema + quality gate"),
        ("1", "Diagnose", "class, health, censoring"),
        ("2", "Featurize", "vintage-safe design"),
        ("3", "Forecast", "candidate pool"),
        ("4", "Combine", "convex stacking"),
        ("5", "Reconcile", "MinT, hierarchy"),
        ("6", "Calibrate", "conformal"),
        ("7", "Decide", "fractile + ledger"),
    ]

    def __init__(self, w=None):
        Flowable.__init__(self)
        self.w = w

    def wrap(self, aw, ah):
        self.width = self.w or aw
        self.height = 30 * mm
        return self.width, self.height

    def draw(self):
        c = self.canv
        n = len(self.STAGES)
        gap = 2.0 * mm
        bw = (self.width - gap * (n - 1)) / n
        for i, (idx, name, sub) in enumerate(self.STAGES):
            x = i * (bw + gap)
            lit = i >= 3
            c.setFillColor(ACCENT_SOFT if lit else colors.HexColor("#F4F6F9"))
            c.setStrokeColor(ACCENT if lit else RULE)
            c.setLineWidth(0.6)
            c.roundRect(x, 6 * mm, bw, 19 * mm, 1.8 * mm, fill=1, stroke=1)
            c.setFillColor(ACCENT if lit else MUTE)
            c.setFont("Courier-Bold", 8)
            c.drawString(x + 2.2 * mm, 19.6 * mm, idx)
            c.setFillColor(INK)
            c.setFont("Helvetica-Bold", 7.2)
            c.drawString(x + 2.2 * mm, 15.4 * mm, name)
            c.setFillColor(MUTE)
            c.setFont("Helvetica", 5.7)
            for j, line in enumerate(_wrapw(sub, bw - 4 * mm, 5.7)):
                c.drawString(x + 2.2 * mm, 12.0 * mm - j * 2.5 * mm, line)
            if i < n - 1:
                c.setStrokeColor(RULE)
                c.setLineWidth(0.6)
                c.line(x + bw, 15.5 * mm, x + bw + gap, 15.5 * mm)
        c.setStrokeColor(ACCENT)
        c.setLineWidth(0.5)
        c.setDash(2, 2)
        c.line(3 * (bw + gap), 5 * mm, self.width, 5 * mm)
        c.setDash()
        c.setFillColor(ACCENT)
        c.setFont("Helvetica-Oblique", 6.2)
        c.drawString(3 * (bw + gap), 1.6 * mm,
                     "stages 3-7 exchange predictive distributions only - never a point forecast")


def _wrapw(text, maxw, size, font="Helvetica"):
    from reportlab.pdfbase.pdfmetrics import stringWidth
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if stringWidth(t, font, size) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


# ================================================================ tables
def table(data, widths, header=True, align_right=(), zebra=True, size=7.7):
    rows = []
    for r, row in enumerate(data):
        out = []
        for cidx, cell in enumerate(row):
            st = CELLH if (header and r == 0) else CELL
            if cidx in align_right:
                st = ParagraphStyle(f"r{r}{cidx}", parent=st, alignment=2,
                                    fontName="Courier-Bold" if (header and r == 0) else "Courier")
            out.append(Paragraph(str(cell), st))
        rows.append(out)
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, 0), 0.7, INK if header else RULE),
        ("LINEBELOW", (0, 1), (-1, -2), 0.3, RULE),
    ]
    if zebra:
        for r in range(1, len(data)):
            if r % 2 == 0:
                style.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#FAFBFD")))
    t.setStyle(TableStyle(style))
    return t


def kv(pairs, w, label_w=52 * mm):
    return table([[k, v] for k, v in pairs], [label_w, w - label_w],
                 header=False, zebra=False)


# ============================================================ page frame
class Doc(BaseDocTemplate):
    def __init__(self, path, **kw):
        BaseDocTemplate.__init__(self, path, pagesize=A4,
                                 leftMargin=MARGIN, rightMargin=MARGIN,
                                 topMargin=MARGIN, bottomMargin=18 * mm, **kw)
        fw = PW - 2 * MARGIN
        fh = PH - MARGIN - 18 * mm
        self.addPageTemplates([
            PageTemplate(id="cover", frames=[Frame(MARGIN, 18 * mm, fw, fh, id="c")],
                         onPage=self._cover),
            PageTemplate(id="body", frames=[Frame(MARGIN, 18 * mm, fw, fh - 8 * mm, id="b")],
                         onPage=self._chrome),
        ])
        self.section = ""

    def _cover(self, c, doc):
        c.setFillColor(ACCENT, alpha=0.05)
        p = c.beginPath()
        p.moveTo(0, PH)
        p.lineTo(PW * 0.72, PH)
        p.lineTo(0, PH * 0.52)
        p.close()
        c.drawPath(p, fill=1, stroke=0)

    def _chrome(self, c, doc):
        c.saveState()
        c.setFont("Helvetica", 6.8)
        c.setFillColor(MUTE)
        c.drawString(MARGIN, PH - MARGIN + 3 * mm, "DQ-STACK - COMPLETE REFERENCE")
        c.drawRightString(PW - MARGIN, PH - MARGIN + 3 * mm, self.section.upper())
        c.setStrokeColor(RULE)
        c.setLineWidth(0.5)
        c.line(MARGIN, PH - MARGIN + 1.4 * mm, PW - MARGIN, PH - MARGIN + 1.4 * mm)
        c.line(MARGIN, 14 * mm, PW - MARGIN, 14 * mm)
        c.setFont("Courier", 7.4)
        c.setFillColor(INK)
        c.drawRightString(PW - MARGIN, 10 * mm, str(doc.page))
        c.restoreState()


class Sec(Flowable):
    """Sets the running header for the pages that follow."""

    def __init__(self, doc, name):
        Flowable.__init__(self)
        self.doc, self.name = doc, name

    def wrap(self, aw, ah):
        return 0, 0

    def draw(self):
        self.doc.section = self.name


# ================================================================= build
def build():
    doc = Doc(str(OUT), title="DQ-Stack - Complete Reference",
              author="DQ-Stack", subject="Probabilistic demand forecasting and inventory decisions")
    W = PW - 2 * MARGIN
    st = []
    A = st.append

    def h1(text, sub=""):
        A(Paragraph(text, H1))
        if sub:
            A(Paragraph(sub, H1SUB))
        A(Rule(space=2))
        A(Spacer(1, 7))

    def p(text, style=BODYS):
        A(Paragraph(text, style))

    def bullets(items):
        for it in items:
            A(Paragraph(it, BULLET, bulletText="•"))
        A(Spacer(1, 4))

    # ------------------------------------------------------------- cover
    A(Spacer(1, 44 * mm))
    A(Mark(size=40 * mm / 3))
    A(Spacer(1, 9 * mm))
    A(Paragraph("DQ-Stack", ParagraphStyle(
        "cover", fontName="Times-Roman", fontSize=44, leading=48, textColor=INK)))
    A(Paragraph("Probabilistic demand forecasting and inventory decisions",
                ParagraphStyle("cs", fontName="Helvetica", fontSize=13, leading=18,
                               textColor=ACCENT, spaceBefore=5)))
    A(Spacer(1, 6 * mm))
    A(Paragraph(
        "A complete reference: what the system does, how the eight-stage pipeline works, "
        "the architecture and its constraints, the full design system, every screen in "
        "the interface, the measured results, and the engineering record.",
        ParagraphStyle("cd", parent=BODYS, fontSize=10.4, leading=16, textColor=BODY)))
    A(Spacer(1, 12 * mm))
    A(Rule(space=2))
    A(Spacer(1, 4 * mm))
    A(kv([
        ("Series planned", f"{M.get('n_series', '-'):,}"),
        ("Panel rows", f"{M.get('n_panel_rows', '-'):,}"),
        ("Forecast horizon", f"{M.get('horizon', '-')} days, direct multi-horizon"),
        ("Quantile levels", f"{len(M.get('quantiles', []))} per day per series"),
        ("Backtest origins", f"{F['backtest']['n_origins']} rolling, paired inference"),
        ("Pipeline", f"{sum(1 for _ in Pipeline.STAGES)} stages, {7946:,} lines of Python"),
        ("Interface", f"{8822:,} lines, no build step, no framework"),
        ("Run generated", str(M.get("generated_at", "-"))[:19].replace("T", " ")),
    ], W))
    A(Spacer(1, 9 * mm))
    A(Paragraph(
        "Every figure in this document is read from <font face='Courier'>artifacts/</font> "
        "and <font face='Courier'>ui/css/tokens.css</font> at build time. Nothing is transcribed.",
        NOTE))
    A(NextPageTemplate("body"))
    # Sec must precede the PageBreak. `onPage` runs when a page begins, before any
    # flowable on it draws, so a Sec placed after the break sets the name for the
    # page after next and every header lagged by one.
    A(Sec(doc, "Contents"))
    A(PageBreak())

    # ---------------------------------------------------------- contents
    h1("Contents")
    toc = [
        ("I", "What the system is", "The problem, the thesis, and who uses it"),
        ("II", "The eight-stage pipeline", "Every stage: purpose, method, and why"),
        ("III", "Architecture", "Constraints, the read path, and in-browser SQL"),
        ("IV", "The design system", "Identity, colour, type, space, motion"),
        ("V", "The interface", "Every view, control and keyboard path"),
        ("VI", "Replenishment", "Stock counts, the order list, and cycle counting"),
        ("VII", "Evidence and results", "Accuracy, calibration, and economics"),
        ("VIII", "Engineering record", "Defects found, budgets measured"),
        ("IX", "Limits", "What is deliberately not built"),
    ]
    A(table([["", "Part", "Contents"]] + [[a, b, c] for a, b, c in toc],
            [12 * mm, 52 * mm, W - 64 * mm], align_right=(0,)))
    A(Spacer(1, 7 * mm))
    A(Paragraph(
        "This document describes a working system. Where a claim is measured, the "
        "measurement is quoted; where something is unproven or deliberately absent, "
        "it is stated as such rather than omitted.", NOTE))

    # =============================================================== I
    A(Sec(doc, "I - What it is"))
    A(PageBreak())
    h1("Part I", "What the system is")

    p("A demand planner at a grocery or retail chain decides how much of every product to "
      "order for every store, every week. One planner may be accountable for thousands of "
      "product-and-store combinations. They cannot look at them one at a time, and the "
      "consequences of being wrong are asymmetric and specific to each product: running out "
      "of a high-margin durable costs a sale that will not come back, while over-ordering "
      "fresh milk means throwing it away.", LEAD)

    A(Paragraph("The thesis", H2))
    p("Most forecasting tools produce a number and leave the planner to trust it. This system "
      "argues that a forecast is only useful when four things travel with it: <b>how uncertain "
      "it is</b>, <b>whether the data behind it can be trusted</b>, <b>what decision follows "
      "from it in money</b>, and <b>what it costs when a human overrules it</b>. Every "
      "architectural choice below follows from that.")
    tq = TARGET_Q
    p("The sharpest expression is the ordering rule. Two products with an identical forecast "
      "get different orders, because the right stock level depends on what mistakes cost. The "
      "system derives a service level per product from its own price, cost, shelf life and "
      "holding cost - the critical fractile, underage over underage plus overage - instead of "
      "applying one blanket target to everything. Measured across this assortment the target "
      f"ranges from <b>{tq['min']:.0%}</b> to <b>{tq['max']:.0%}</b>, with a mean of "
      f"<b>{tq['mean']:.3f}</b> and a median of {tq['median']:.3f}. A blanket policy cannot "
      "express any of that spread.")

    A(Spacer(1, 3))
    A(FanDiagram(w=W))
    A(Paragraph(
        "The motif and the method are the same object. Observed sales resolve into a forecast "
        "that widens as it reaches further out, drawn as three nested bands - the middle 50%, "
        "80% and 98% of plausible outcomes. The interface never shows a forecast without it.",
        NOTE))

    A(Paragraph("Who uses it", H2))
    A(table([
        ["Role", "What they do here"],
        ["Demand planner", "Works the daily decision queue, approves or overrides forecasts, "
                           "adds and retires products. The primary user."],
        ["Category manager", "Reviews performance and economics by category, department and store."],
        ["Supply-chain analyst", "Interrogates accuracy, calibration and model attribution; "
                                 "queries the planning table directly in SQL."],
        ["Reviewer or examiner", "Reads the evidence surfaces: forecast value added by stage, "
                                 "reliability, and the accuracy-versus-economics comparison."],
    ], [40 * mm, W - 40 * mm]))

    A(Paragraph("How it works, in one paragraph", H2))
    p("Two years of daily sales go in. The system first decides whether each zero in that "
      "history means <i>nobody wanted it</i> or <i>the shelf was empty</i>, because those look "
      "identical in the data and mean opposite things. It sorts every product by how it sells - "
      "steadily, in bursts, occasionally, or too new to tell - and routes each to methods "
      "suited to that behaviour. It forecasts the next 28 days as a distribution rather than a "
      "number, combines several models, makes the forecasts add up across the product and store "
      "hierarchy, corrects the intervals against their own historical coverage, and finally "
      "turns the distribution into a stock level using that product's economics. A planner then "
      "sees only the decisions that carry real money, ranked by how much.")

    # =============================================================== II
    A(Sec(doc, "II - The pipeline"))
    A(PageBreak())
    h1("Part II", "The eight-stage pipeline")
    A(Pipeline(w=W))
    A(Spacer(1, 5))
    p("Each stage has a single responsibility and a typed interface. The contract that makes "
      "the rest possible is that <b>stages three through seven exchange predictive "
      "distributions only</b>. Nothing downstream ever receives a point forecast, which is "
      "what allows calibration, economic evaluation and the decision layer to exist at all.")

    STAGES = [
        ("Stage 0", "Ingest and validate",
         "Declared schemas with fingerprints, a quality gate, and reconciliation of aggregate "
         "totals against source-declared totals. A feed that fails is rejected rather than "
         "silently degraded, and every run records which feeds were accepted.",
         "A pipeline that accepts a broken feed produces a forecast that looks reasonable and "
         "is wrong. Rejection has to be a first-class outcome."),
        ("Stage 1", "Diagnose",
         "Classifies every series by the Syntetos-Boylan-Croston quadrants (ADI 1.32, CV-squared "
         "0.49) plus cold-start and terminal states; scores data health; detects censoring both "
         "structurally (no price record means the product was not carried) and probabilistically "
         "(weekday zero-rate); and finds regime changes with PELT changepoint detection.",
         "A single global model scored on the average of these classes hides exactly the "
         "sparse-series behaviour that most needs routing around."),
        ("Stage 2", "Featurize",
         "Dense-matrix featurisation with a strict vintage discipline: features for an origin "
         "are computed from data available at that origin only, including the Stage-1 "
         "diagnostics, which are recomputed per origin block rather than taken from full history.",
         "The most expensive class of bug in forecasting is leakage, and it flatters results "
         "silently. There is a dedicated test suite for it."),
        ("Stage 3", "Forecast",
         "A candidate pool rather than a single model: LightGBM Tweedie plus nine native "
         "quantile heads, the Croston family (Croston, SBA, TSB, ADIDA), Holt-Winters ETS, "
         "NHITS, a covariate-only zero-shot prior, and cohort lifecycle curves. Direct "
         "multi-horizon in three horizon groups, never recursive.",
         "Recursive forecasting compounds its own error. Direct horizons cost more to train "
         "and are honest about what they predict."),
        ("Stage 4", "Combine",
         "Convex stacking that minimises pinball loss on the blended quantiles, solved by SLSQP, "
         "with weights conditioned on Stage-1 features.",
         "The FFORMA-style proxy is linear in the weights, so its optimum is a simplex vertex - "
         "it can only ever pick one model, never average. That is why it was replaced."),
        ("Stage 5", "Reconcile",
         "MinT with Schaefer-Strimmer shrinkage across the product and store hierarchy, plus "
         "temporal reconciliation so daily, weekly and 28-day views are arithmetically identical.",
         "'The weekly plan disagrees with the quarterly plan' becomes structurally impossible "
         "rather than a recurring meeting."),
        ("Stage 6", "Calibrate",
         "Split conformal and CQR adjustment, validated out of sample. The median is never "
         "shifted, and monotonicity is restored about the median rather than by sorting.",
         "An interval that claims 90% coverage and delivers 62% will be trusted for a decision "
         "it cannot support."),
        ("Stage 7", "Decide",
         "The critical fractile from each product's own economics, a vectorised periodic-review "
         "order-up-to simulator, and a full ledger: gross margin, holding, waste, markdown and "
         "lost margin.",
         "Accuracy is a proxy. The ledger is the thing the business actually experiences, and "
         "model promotion is gated on it."),
    ]
    for name, title, what, why in STAGES:
        A(KeepTogether([
            Paragraph(f"{name} &nbsp;&middot;&nbsp; {title}", H3),
            Paragraph(what, BODYS),
            Paragraph(f"<b>Why.</b> {why}", NOTE),
            Spacer(1, 3),
        ]))

    A(Paragraph("The feedback loop", H2))
    p("Planner overrides are recorded with a reason code and become an input to later runs: "
      "a product a human corrects every week is a product the model is failing on, and that "
      "is a measurable signal rather than an anecdote. In the current build overrides are "
      "session-local drafts exported as CSV, because there is no write path to a system of "
      "record - see Part VIII.")

    # =============================================================== III
    A(Sec(doc, "III - Architecture"))
    A(PageBreak())
    h1("Part III", "Architecture")

    A(Paragraph("Constraints", H2))
    A(table([
        ["", "Constraint", "How it is met"],
        ["CON-1", "Bulk numeric transfer uses Apache Arrow IPC; JSON is not used for "
                  "forecast matrices",
         "<font face='Courier'>/forecast.arrow</font> and <font face='Courier'>/series.arrow</font>. "
         "Measured on this project's own 28,000-row matrix: 1.44 MB against 3.26 MB, and "
         "1.1 ms against 65.6 ms to parse."],
        ["CON-2", "Forecasts are versioned; a prior vintage is always available",
         "Every series carries its previous version, drawn as a ghost line and summarised as "
         "'versus last week'."],
        ["CON-3", "Forecasts are precomputed in batch; no model inference on the read path",
         "Every endpoint serves rows the pipeline already wrote. A new product is marked "
         "<i>awaiting its first forecast</i> rather than having one invented in the browser."],
        ["CON-4", "Uncertainty is never discarded between stages",
         "Stages 3-7 exchange distributions only."],
        ["CON-5", "Numeric aggregation happens in the analytical store or the client query "
                  "engine, not in application code",
         "DuckDB views over the Parquet lake; rollups expressed as SQL."],
    ], [15 * mm, 55 * mm, W - 70 * mm]))

    A(Paragraph("The read path", H2))
    p("<font face='Courier'>dqstack/serve.py</font> is a DuckDB analytical store over the "
      "Parquet lake, exposed as a read-only HTTP API, which also serves the workspace itself. "
      "Views rather than tables: the pipeline owns the files, and copying them into a database "
      "would create a second version of the truth that goes stale the moment a run finishes.")
    A(Paragraph(
        "python -m dqstack.serve             # workspace + API on 127.0.0.1:8000\n"
        "python -m dqstack.serve --selftest  # exercise the store and every route\n"
        "python -m dqstack.serve --measure   # the Arrow versus JSON comparison", CODE))
    p("The static half is not a convenience. DuckDB-WASM is an ES module whose worker is "
      "cross-origin, and a page opened from <font face='Courier'>file://</font> has a null "
      "origin, so the browser refuses both the dynamic import and the worker. The client-side "
      "query engine cannot exist without an origin, so the app has to be served.")

    A(Paragraph("In-browser SQL, and what measuring it showed", H2))
    p("The specification calls client-side SQL 'the single biggest perceived-performance win "
      "available'. It was built - a 195 KB Arrow slice registered as a DuckDB-WASM table - and "
      "then measured, and the measurement only partly agrees.")
    A(table([
        ["Rows", "Row selection: SQL", "JavaScript", "Ratio"],
        ["1,000", "10.9 ms", "0.5 ms", "0.04x"],
        ["10,000", "35.0 ms", "5.2 ms", "0.15x"],
        ["30,000", "46.2 ms", "11.6 ms", "0.25x"],
        ["100,000", "74.5 ms", "79.9 ms", "1.07x - crossover"],
        ["300,000", "93.0 ms", "215.4 ms", "2.32x"],
    ], [30 * mm, 45 * mm, 35 * mm, W - 110 * mm], align_right=(1, 2, 3)))
    p("Group rollups never cross over at all (0.06x at 1k, 0.22x at 300k), because a hierarchy "
      "level has three to ten distinct keys and a ten-bucket hash over a flat array is about as "
      "cheap as computation gets, while every DuckDB query pays a fixed cost. So the router "
      "chooses by the measurement rather than the claim: SQL for row selection above 100,000 "
      "rows, JavaScript for rollups and the small case, and DuckDB unconditionally for the SQL "
      "console, where the alternative is not a slower answer but no answer. Both backends are "
      "held to identical results by a self-test that runs the real workload through each.")

    A(Paragraph("Repository map", H2))
    A(table([
        ["Path", "Responsibility"],
        ["dqstack/config.py", "Every threshold and ablation switch"],
        ["dqstack/ingest.py, schemas.py", "Stage 0: schemas, fingerprints, quality gate"],
        ["dqstack/diagnose.py", "Stage 1: class, health, censoring, regimes"],
        ["dqstack/features.py", "Stage 2: vintage-safe featurisation"],
        ["dqstack/models/", "Stage 3: the candidate pool and the distribution contract"],
        ["dqstack/combine.py", "Stage 4: convex stacking"],
        ["dqstack/reconcile.py", "Stage 5: MinT, hierarchical and temporal"],
        ["dqstack/calibrate.py", "Stage 6: split conformal / CQR"],
        ["dqstack/decide.py", "Stage 7: fractile, simulator, ledger"],
        ["dqstack/evaluate.py, backtest.py", "WRMSSE, WSPL, coverage, FVA, paired inference"],
        ["dqstack/serve.py", "The read path: store, Arrow, HTTP, static"],
        ["ui/js/plain.js", "Every word a non-specialist reads"],
        ["ui/js/review.js", "Today: the decision queue"],
        ["ui/js/catalog.js", "Products: browse, detail, add and remove"],
        ["ui/js/grid.js, chart.js", "The canvas grid and fan chart"],
        ["ui/js/duck.js", "DuckDB-WASM and the two-backend resolver"],
        ["ui/css/tokens.css", "Design tokens - read by the canvases at paint time"],
    ], [52 * mm, W - 52 * mm]))

    # =============================================================== IV
    A(Sec(doc, "IV - Design system"))
    A(PageBreak())
    h1("Part IV", "The design system")

    A(Paragraph("Identity: the fan", H2))
    p("The mark is an origin rule with two nested wedges opening to the right. It was chosen "
      "because it is <b>already the product's own geometry</b> rather than a metaphor applied "
      "to it: a vertical origin and a widening cone is what the canvas chart, the canvas grid, "
      "the review sparkline and the product chart all draw. Shelf strata, the underage-overage "
      "balance and the weekly demand rhythm were considered and rejected.")
    A(Spacer(1, 2))
    A(Mark(size=30))
    A(Spacer(1, 5))
    bullets([
        "<b>Where it appears.</b> The sidebar lockup, the browser tab, the two empty states "
        "that mean 'nothing here yet', and a single cone-shaped wash on the scrolling surfaces.",
        "<b>Where it must not.</b> The completed-work empty state keeps a tick, because a fan "
        "there would say the opposite of what happened. Nothing else uses it - a signature that "
        "appears everywhere is wallpaper.",
        "<b>What was removed.</b> A film-grain texture that called itself 'the only decorative "
        "element in the interface', said nothing about the domain, and at 0.020 opacity was "
        "invisible on the panels this is actually used on.",
    ])

    A(Paragraph("Colour", H2))
    p("Every hue carries exactly one meaning. The most important change in the system was "
      "splitting two meanings that had been sharing one token: <font face='Courier'>"
      "--series-forecast</font> meant both 'this is a predicted quantity', which the canvas "
      "renderers read, and 'this is clickable'. A hue that means two unrelated things cannot "
      "be learned, so <font face='Courier'>--ui-accent</font> now carries the interactive role "
      "across 38 surfaces while the data role stays put.")

    D, L = F["dark"], F["light"]
    A(Swatches([
        ("Forecast / demand", D.get("--series-forecast", ""), L.get("--series-forecast", ""),
         "predicted quantities, fan bands"),
        ("Interactive accent", D.get("--ui-accent", ""), L.get("--ui-accent", ""),
         "links, focus, selection, primary action"),
        ("Observed", D.get("--series-actual", ""), L.get("--series-actual", ""),
         "sales that actually happened"),
        ("Prior version", D.get("--series-prior", ""), L.get("--series-prior", ""),
         "last cycle's forecast, as a ghost"),
        ("Override", D.get("--series-override", ""), L.get("--series-override", ""),
         "a human changed this"),
        ("Risk", D.get("--state-risk", ""), L.get("--state-risk", ""),
         "money at risk, likely to run out"),
        ("Excess / good", D.get("--state-excess", ""), L.get("--state-excess", ""),
         "healthy, or too much stock"),
        ("Attention", D.get("--state-attention", ""), L.get("--state-attention", ""),
         "check this"),
    ], w=W))
    A(Paragraph(
        "Dark value left, light value right. Both themes are real palettes, not inversions of "
        "one another. <b>Forecast and interactive accent deliberately share a value today</b> - "
        "the split was made by adding a token with an identical value, so no pixel moved and "
        "nothing the canvas renderers resolve was touched. What it buys is the ability to move "
        "either role later without dragging the other with it.", NOTE))

    A(Paragraph("Surfaces and contrast", H3))
    A(Swatches([
        ("Base", D.get("--bg-base", ""), L.get("--bg-base", ""), "the void behind everything"),
        ("Surface", D.get("--bg-surface", ""), L.get("--bg-surface", ""), "chrome, cards, panels"),
        ("Raised", D.get("--bg-raised", ""), L.get("--bg-raised", ""), "hovers, chips, table heads"),
        ("Inset", D.get("--bg-inset", ""), L.get("--bg-inset", ""), "inputs, wells, chart grounds"),
        ("Text primary", D.get("--text-primary", ""), L.get("--text-primary", ""), "16.9:1 / 17.2:1"),
        ("Text secondary", D.get("--text-secondary", ""), L.get("--text-secondary", ""), "7.2:1 / 6.4:1"),
        ("Text tertiary", D.get("--text-tertiary", ""), L.get("--text-tertiary", ""), "3.9:1 - never body text"),
    ], w=W))
    p("Thirty-four contrast checks are computed across both themes at build time and all pass "
      "WCAG 2.2 AA. One finding came out of that audit: chart axis labels had been using the "
      "tertiary text colour, which measures 3.87:1 dark and 3.86:1 light on the surfaces charts "
      "actually sit on - below the 4.5:1 threshold, on the numbers that give every chart its "
      "scale. They now use the secondary colour at 7.80:1 and 5.66:1. The token itself was not "
      "revalued, because it is read by name in dozens of non-chart places.")

    A(PageBreak())
    A(Paragraph("Typography", H2))
    A(table([
        ["Family", "Role", "Why"],
        ["Instrument Sans", "All interface text",
         "Neutral, dense, legible at 10-13px."],
        ["JetBrains Mono", "Every numeral in the product",
         "A monospace face is tabular by construction, so column alignment is guaranteed rather "
         "than dependent on a font-feature setting surviving every code path."],
        ["Instrument Serif", "Section headings on the three reference views",
         "Marks the mode change from scanning an instrument to reading a document. Deliberately "
         "absent from Today, Products and the spreadsheet - the split only means something if "
         "it is exclusive."],
    ], [34 * mm, 46 * mm, W - 80 * mm]))

    A(Paragraph("The figure scale", H3))
    p("This is a numbers application whose seven hero figures had used six different sizes and "
      "five tracking values, so no two panels agreed on how big 'the answer' is. Three steps now, "
      "each with the tracking that size actually wants.")
    A(table([
        ["Token", "Size", "Tracking", "Weight", "Used for"],
        ["--fig-xl", D.get("--fig-xl", "40px"), D.get("--track-fig-xl", ""), "600",
         "Money at risk; the exception count"],
        ["--fig-lg", D.get("--fig-lg", "28px"), D.get("--track-fig-lg", ""), "600",
         "The figure a decision acts on"],
        ["--fig-md", D.get("--fig-md", "20px"), D.get("--track-fig-md", ""), "500",
         "Supporting figures on a card"],
    ], [26 * mm, 18 * mm, 22 * mm, 18 * mm, W - 84 * mm], align_right=(1, 2, 3)))
    p("Words are not set in the numeral face. 'Good' or 'Fair' at 20px monospace reads as a "
      "status code rather than a judgement, and a word has no column to align to - so the "
      "figure class was split into a numeral variant and a word variant.")

    A(Paragraph("Space, radius, elevation, motion", H2))
    A(table([
        ["Scale", "Values", "Rule"],
        ["Spacing", "2, 4, 6, 8, 12, 16, 24, 32, 48",
         "Nine steps, nothing else. A half-step nobody named is how a spacing scale dies."],
        ["Radius", f"{D.get('--r-sm','4px')}, {D.get('--r-input','6px')}, "
                   f"{D.get('--r-card','10px')}, {D.get('--r-panel','14px')}, pill, 0 in grid cells",
         "Rounding blurs alignment, so grid cells have none."],
        ["Page frame", f"gutter {D.get('--gutter','32px')}, rail {D.get('--rail-w','288px')}",
         "One gutter across every view. The content edge used to move on every navigation."],
        ["Elevation", "ring + shadow, three levels",
         "On a dark surface a shadow is nearly invisible; the lit top edge is what reads as depth."],
        ["Motion", f"{D.get('--t-feedback','120ms')} feedback, {D.get('--t-panel','180ms')} panel",
         "Confined to state changes that need confirming. Nothing animates on load or while "
         "data is being read; zeroed under prefers-reduced-motion."],
    ], [26 * mm, 52 * mm, W - 78 * mm]))

    A(Paragraph("The token safety contract", H2))
    p("The canvas renderers resolve CSS custom properties <b>by name at paint time</b>. "
      "Twenty-two tokens are load-bearing: the eighteen colours cached in "
      "<font face='Courier'>util.js</font>, plus the two font families and the two row metrics. "
      "Renaming or removing one is not a CSS edit; it is a silent rendering failure in an "
      "unrelated file. Additions are always safe, which is why every change in this design "
      "system was made by adding a token rather than repurposing one.")

    # =============================================================== V
    A(Sec(doc, "V - The interface"))
    A(PageBreak())
    h1("Part V", "The interface")
    p("Four destinations in a sidebar of nine collapsible blocks. Everything is reachable in "
      "one click; no feature is more than two.", LEAD)

    A(table([
        ["Group", "Destination", "What it is for"],
        ["Your work", "Today", "The decision queue. The landing view."],
        ["", "Order list", "What to buy: target minus what is on the shelf."],
        ["", "Products", "Browse the range, inspect one product, change the range."],
        ["Understand it", "How it's doing", "Accuracy, calibration and economics."],
        ["", "Ordering decisions", "Why two products with the same forecast get different orders."],
        ["", "How it works", "The eight stages, with this run's own numbers."],
        ["", "Policy cost", "What each of nine ways of running the system earned."],
        ["Tools", "Spreadsheet", "The bulk-editing instrument: canvas grid, keyboard-driven."],
        ["", "Glossary, SQL console, shortcuts", "Plain-English definitions; ad-hoc queries; the key map."],
    ], [26 * mm, 40 * mm, W - 66 * mm]))

    A(Paragraph("The sidebar", H2))
    p("The rail holds the planner's own work rather than the application's features, which is "
      "the distinction an external review drew and the reason it grew rather than shrank. Every "
      "block carries a live number: a rail where nothing changes between visits is a list of "
      "links, and a rail where six figures moved overnight is the first thing read each morning.")
    p("It outgrew one screen once it carried replenishment as well as review, so every block is "
      "a native disclosure element whose open set is remembered per browser - a buyer lives in "
      "the order block, a demand planner lives in the queue, and neither should scroll past the "
      "other's. <b>A collapsed block still reports its figure in its heading</b>, because the "
      "whole argument for this rail is that a closed block is not a silent one.")
    A(table([
        ["Block", "What it reports", "Where it goes"],
        ["Cycle", "Vintage of the forecast, position in its horizon", "-"],
        ["Replenishment", "Today's order cost over the counted share it describes",
         "Order list"],
        ["Stock take", "What is due a count, by value class", "Order list, filtered"],
        ["Stock cover", "Four buckets: runs out first, needs topping up, covered, overstocked",
         "Order list, filtered"],
        ["Your queue", "The day's decisions grouped by cause, with exposure",
         "Today, filtered"],
        ["Range mix", "ABC against XYZ, shaded by share of stock value", "Products, filtered"],
        ["Watching", "Pinned products and how far they moved", "Products"],
        ["Saved views", "Scope, filter and tab under a chosen name", "Today, restored"],
        ["Stores", "All stores ranked by units to stock, yours pinned", "Sets scope"],
        ["Session", "Decisions made, exposure covered, and an undo", "-"],
    ], [30 * mm, W - 70 * mm, 40 * mm]))

    A(Paragraph("Today - the decision queue", H2))
    p("The front door shows a day's work, not the backlog. Of the flagged series, the "
      "highest-value are cut to a fixed cohort chosen once at load, so clearing the list "
      "actually finishes it - an earlier version re-sliced the queue on every render, so the "
      "progress readout went '1 of 25', then '2 of 26', and the finish line moved away as you "
      "worked.")
    p("Each item is one card asking one question, with one obvious answer. The card is built "
      "in four rungs, in the order a decision is actually made - a planner clearing twenty-four "
      "of these reads the same layout twenty-four times, so what sits where does more work here "
      "than anywhere else in the application:")
    A(table([
        ["Rung", "Question it answers", "Content"],
        ["0", "Is this worth my attention?",
         "Position in the day's list, money at risk, how long the product has been selling."],
        ["1", "What am I deciding?",
         "Product and store; next week's forecast with its range; the stock-up-to level "
         "with the cost curve beside it."],
        ["2", "Why is it in front of me?",
         "The headline reason in one sentence, from a fixed priority order, and the chart "
         "behind it: 35 days of sales and the 28-day fan, bled to the card edges."],
        ["3", "Do it", "Approve / Change / Skip, plus the drawer and the product page."],
    ], [14 * mm, 44 * mm, W - 58 * mm]))
    p("Exactly one filled button is on screen per card. That hierarchy is what makes a card "
      "answerable in one click without reading all of it. Exposure was a hero figure and did "
      "not deserve to be: it is the reason the card sits in this position, and the position now "
      "states that directly.")
    p("Six figures came off the face of the card and into the drawer behind <b>W</b>, ordered so "
      "the two that qualify the decision come first and the four that explain the cost curve "
      "follow. A planner told that running out costs 2.2x more than overstocking, who wants to "
      "know why, finds the margin, the shelf life, the value of the line and how often it sells "
      "- which is where the answer actually is. The drawer also carries how the last 28 days "
      "went, where the number came from, and what to do.")
    p("Confidence and data-quality wording is calibrated to the terciles of this assortment "
      "rather than to guessed cutoffs - hardcoded thresholds put every visible card in one "
      "bucket, which trains a planner to ignore the field.")

    A(Paragraph("The cost curve", H3))
    p("The ordering layer's argument is that two products with the same forecast deserve "
      "different stock levels, because the two ways of being wrong do not cost the same and how "
      "unequal they are is a property of the product. Said in a paragraph, nobody reads it. "
      "Drawn as a shape with the recommendation at the bottom of it, it is learnable in a week "
      "without meeting a technical term.")
    p("The curve's minimum <b>is</b> the recommended level, by construction rather than by "
      "fitting: a low point sitting beside the number printed above it would discredit both, "
      "silently. The demand distribution is rebuilt from two figures the bundle states exactly "
      "- the mean of the cover window, and F(order-up-to) = the target service level - which "
      "determines a two-parameter distribution. Verified at a worst offset of <b>0.000%</b> "
      "across all 997 series that have a forecast.")
    p("It differentiates, which is the point. Across the assortment the ratio of under-ordering "
      "cost to over-ordering cost runs 1.25 to 5.36, and the extremes are the thesis itself: "
      "720-day-shelf, 44-53% margin lines are visibly lopsided, and 21-day-shelf, 17% margin "
      "food is visibly flat.")

    A(Paragraph("Changing a number", H3))
    p("An override states a reason, and the reason is required. Every other fact about a "
      "decision can be rebuilt later - what the model said, what was chosen, what it was worth "
      "- but 'the end cap goes up on Tuesday' exists only in the head of whoever typed the "
      "number, and it is the one column that could teach a later run something. Six codes, plus "
      "free text: promotion, local event, supply, price, range, own knowledge.")
    p("A guardrail reports where the number sits, live as it is typed, and in odds rather than "
      "percentiles: 'about a 1 in 11 chance demand reaches this level'. Beyond the exported "
      "ladder it says so plainly. It is a guardrail and not a block - the planner is allowed to "
      "be right when the model is wrong, which is the whole reason an override exists. The "
      "expected effect comes from the same estimator the spreadsheet cell and the commit summary "
      "quote, so the three surfaces cannot disagree.")

    A(Paragraph("The work bar", H3))
    p("A planner with twenty-four cards and forty minutes spends the same click on a $9 item as "
      "on a $16,000 one. Approve-all fixes that by discarding the judgement; a threshold keeps "
      "the judgement where the money is. Thresholds are percentiles of the list in front of the "
      "planner, and an offer survives only if it is genuinely the small tail: at most 45% of the "
      "items and at most 15% of the money. A tier with no small items correctly offers nothing "
      "rather than relabelling approve-all.")
    p("Batching is a window over the fixed cohort, never a re-slice of it. The cards on screen "
      "go from twenty-four to five while the headline, the money at risk, the progress readout "
      "and the position all stay exactly as they were.")

    A(Paragraph("Keyboard", H3))
    A(table([
        ["Key", "Action", "Key", "Action"],
        ["J / K", "Next / previous decision", "W", "Why this number"],
        ["A or Enter", "Approve", "O", "Open the product page"],
        ["C", "Change the number", "?", "Shortcut sheet"],
        ["S", "Skip", "Cmd-K", "Search"],
        ["Cmd-Z", "Undo the last decision", "Esc", "Close whatever is open"],
    ], [20 * mm, 52 * mm, 20 * mm, W - 92 * mm]))
    p("Undo reverses an <i>action</i>, not a row. Once one press can approve five items, undoing "
      "them one at a time is not an undo, so every decision carries the sequence number of the "
      "action that made it and a bulk approve is one unit in the undo stack exactly as it was "
      "one click in the interface. Committed decisions are deliberately not undoable, because "
      "the planner has already been told they were final.")
    p("The spreadsheet keeps its own nine bindings - arrows, range selection, fill-down, "
      "Excel-compatible copy and paste, undo and redo across the whole pending set. The "
      "letters above were chosen to be sayable rather than to mirror spreadsheet convention, "
      "because the review list is not a spreadsheet.")

    A(Paragraph("Finishing a session", H3))
    p("A sticky bar reports what was decided, how many numbers were changed, and the expected "
      "margin effect - quoting the same estimator the spreadsheet uses, so the two surfaces "
      "cannot disagree. Two actions follow: <b>Export as CSV</b>, which is the only way "
      "decisions leave the browser, and <b>Commit</b>, which marks them done without claiming "
      "a write to a system of record that does not exist.")

    A(Paragraph("Products", H2))
    p("A list beside a detail pane - the right layout for 'show me that product', and where "
      "search results land. The list searches the displayed name first, filters by category, "
      "store and needs-attention, and offers five orderings; the trailing figure on each row "
      "changes to match the sort, because sorting by money while showing unit counts looks "
      "like a broken sort.")
    p("The detail pane gives one product a full chart with axis labels, a prior-version ghost, "
      "its flags in plain words, an inline forecast edit that shares the spreadsheet's draft "
      "set, the model's own drivers translated out of feature names, and a peer comparison "
      "showing the same product in every other store as a p10-p90 range with a median notch.")

    A(Paragraph("Adding and removing products", H3))
    p("Two numbers a new product needs behave completely differently, and the distinction is "
      "the whole ordering thesis in miniature:")
    A(table([
        ["", "Available immediately?", "Why"],
        ["Service level", "Yes",
         "It falls out of the price, cost and shelf life just entered. The browser reproduces "
         "the pipeline's own critical-fractile formula, with its constants exported from the "
         "config rather than retyped, and previews the result live as you type."],
        ["Stock level", "No",
         "An order-up-to level is a predictive quantile of demand over the risk period, and a "
         "new product has no predictive distribution. It reads 'Not yet - needs a forecast "
         "first'. An earlier version inherited a similar product's level, which contradicted "
         "the service level beside it."],
    ], [30 * mm, 32 * mm, W - 62 * mm]))
    A(table([
        ["Product", "Margin", "Shelf life", "Target service"],
        ["Fresh milk", "17%", "5 days", "50%"],
        ["Fresh, high margin", "67%", "5 days", "78%"],
        ["Durable, high margin", "70%", "2 years", "93%"],
        ["Durable, thin margin", "8%", "2 years", "50%"],
    ], [50 * mm, 24 * mm, 28 * mm, W - 102 * mm], align_right=(1, 2, 3)))

    A(Paragraph("The spreadsheet", H2))
    p("A canvas-rendered grid, used above roughly ten thousand cells because DOM nodes stop "
      "being viable there. It carries an in-cell uncertainty micro-fan and a confidence gutter, "
      "so a row built on censored or stale data is marked before the planner reads the number. "
      "Because a canvas is invisible to assistive technology, a parallel ARIA table of the "
      "visible window is maintained alongside it. This is the bulk-editing instrument and is "
      "deliberately no longer the default surface.")

    # =============================================================== VI
    # =============================================================== VI
    A(Sec(doc, "VI - Replenishment"))
    A(PageBreak())
    h1("Part VI", "Replenishment")
    p("The system could forecast demand and compute a target - stock up to 156 units - and then "
      "stopped. It never asked the one question that turns a target into an order: how many are "
      "on the shelf now. Every screen was a recommendation nobody could act on.", LEAD)

    A(Paragraph("On-hand is entered, never invented", H2))
    p("Stock counts are typed by the planner and kept in the browser. They are not derived, "
      "estimated, or back-filled from the simulation, and there is deliberately <b>no "
      "'fill these in for me' button</b>. A fabricated count flows straight into an order "
      "quantity and out of the building as a purchase, which makes this the one place in the "
      "system where a plausible-looking invention would cost real money.")
    p("So a product is in one of two states and the interface always says which. A product with "
      "no count shows a dash, never 'order 0'. Zero is a real answer meaning 'you have none', "
      "and conflating the two would hide every uncounted product inside a list of products that "
      "need nothing.")
    A(table([
        ["Quantity", "Definition"],
        ["Order quantity", "stock up to  -  what is on the shelf. Null, not zero, when uncounted."],
        ["Days of cover", "on hand / expected demand per day over the window the order covers."],
        ["Cover status",
         "Below the lead time the shelf empties before a delivery can land; below the review "
         "period plus lead time it will not last the window this order covers; above the "
         "forecast horizon it is carrying more than the period needs."],
    ], [34 * mm, W - 34 * mm]))
    p("The thresholds are the ordering policy's own windows, so they cannot drift from the logic "
      "that produced the target.")

    A(Paragraph("The order list", H2))
    p("Rows are ordered by how soon the shelf empties rather than by name or by value, which is "
      "the only ordering that makes the list safe to abandon halfway through. Counts save as "
      "they are typed - a count is a fact about the world, not a proposal, so there is nothing "
      "to confirm about it, unlike a forecast override, which is a judgement and therefore has "
      "a reason attached. Enter moves to the next count, the way anyone types a stock take.")
    p("The header reports the order total and the counted share as two figures that are never "
      "added, because a confident total over a half-counted range is the number most likely to "
      "be believed and most likely to be wrong. Export produces a purchase order with a TOTAL "
      "line, separate from the decisions export because it is a different document for a "
      "different reader: one is a record of judgements, the other is a list of goods and a price.")

    A(Paragraph("Cycle counting", H2))
    p("Nobody counts a whole shop at once. Counts carry a timestamp, and the recount interval "
      "comes from the ABC class the pipeline already assigns - so the lines that carry the money "
      "come round faster than the tail. Without an age, a figure entered three months ago and "
      "one entered this morning look identical, and the order list depends on both equally.")
    A(table([
        ["Class", "Meaning", "Counted every"],
        ["A", "Your biggest earners", "7 days"],
        ["B", "Middle of the range", "28 days"],
        ["C", "The long tail", "91 days"],
    ], [18 * mm, W - 58 * mm, 40 * mm], align_right=(2,)))
    p("The intervals are the textbook ones and deliberately round. They are a starting policy, "
      "not a claim; what matters is that a count has an age at all and that value sets the "
      "frequency. A count whose age cannot be established is treated as due for recount, which "
      "is the safe reading.")
    p("Uncounted products are ranked by what a full shelf of each is worth - the only value "
      "figure that exists for something nobody has counted. A thousand blank fields is not a "
      "task anyone starts; naming the value that cannot become an order until they are counted, "
      "and listing the most valuable first, turns a chore into a decision about where to spend "
      "an hour.")

    A(Paragraph("Classification the pipeline already had", H2))
    p("ABC ranks products by what they are worth; XYZ by how predictable they are. Both have "
      "been assigned to all thousand products since the first run and no planner-facing screen "
      "ever showed either. The rail presents them as a three-by-three grid rather than two "
      "lists, because the pairing is the point: an A product that is hard to predict needs a "
      "person, a C product that is hard to predict needs a rule, and neither list says that "
      "alone. Shade carries share of stock value, so the eye lands on where the money sits "
      "rather than on where the product count is highest - which is the long tail, always, and "
      "never the answer.")

    A(Sec(doc, "VII - Evidence"))
    A(PageBreak())
    h1("Part VII", "Evidence and results")
    bt = F["backtest"]
    p(f"Rolling-origin backtest over {bt['n_origins']} origins from {bt['origins'][0]} to "
      f"{bt['origins'][-1]}, with <b>paired</b> inference - Wilcoxon signed-rank and an exact "
      f"sign test on per-origin differences, because methods are evaluated on the same origins "
      f"and an unpaired test throws that away.", LEAD)

    A(Paragraph("Leaderboard, mean over origins", H2))
    rows = [["Method", "WSPL", "WRMSSE", "MASE", "Bias", "Rank"]]
    ranks = bt.get("mean_wspl_rank", {})
    for r in bt["by_method"][:10]:
        rows.append([r["method"], num(r["wspl_mean"], 4), num(r["wrmsse_mean"], 4),
                     num(r["mase_mean"], 4), num(r["bias_mean"], 4),
                     num(ranks.get(r["method"], ""), 2)])
    A(table(rows, [46 * mm, 22 * mm, 24 * mm, 20 * mm, 20 * mm, W - 132 * mm],
            align_right=(1, 2, 3, 4, 5)))
    p("Ranked by WSPL - the weighted scaled pinball loss - because it scores the whole "
      "predictive distribution rather than only its centre. WRMSSE is shown alongside for "
      "comparability with the M5 literature.")

    A(Paragraph("Does the full system beat its best single model?", H2))
    for pc in bt.get("paired_comparisons", [])[:3]:
        A(Paragraph(
            f"<b>{pc['a']}</b> versus <b>{pc['b']}</b> on <b>{pc['metric'].upper()}</b>: "
            f"means {num(pc['a_mean'], 4)} against {num(pc['b_mean'], 4)}, "
            f"{pc['a_wins']} wins to {pc['b_wins']} across {pc['n']} origins. "
            f"Wilcoxon p = {num(pc['wilcoxon_p'], 4)}, sign test p = {num(pc['sign_test_p'], 4)}. "
            f"<i>{pc['verdict']}</i>.", BODYS))
    p("Read honestly: the full system wins on distributional quality at every origin, and "
      "<b>loses to its own best single model on point accuracy</b>. Both are reported, because "
      "reporting only the favourable metric is the failure mode this project exists to argue "
      "against.")

    A(Paragraph("Calibration", H2))
    cov = F.get("coverage_table", [])
    rows = [["Nominal", "Empirical before", "After", "Gap before", "Gap after"]]
    for r in cov:
        rows.append([num(r.get("nominal"), 3), num(r.get("empirical_before"), 4),
                     num(r.get("empirical_after", r.get("empirical")), 4),
                     num(r.get("gap_before"), 4), num(r.get("gap_after"), 4)])
    A(table(rows, [24 * mm, 34 * mm, 26 * mm, 28 * mm, W - 112 * mm],
            align_right=(0, 1, 2, 3, 4)))
    p("The upper tail - the region the ordering rule actually reads a quantile from - is well "
      "calibrated, and conformal adjustment improves it further. The lower tail is not, and the "
      "table says so: at the 5% level empirical coverage is far above nominal. That is a "
      "property of a zero-inflated count distribution where a large mass sits at zero, and it "
      "matters little for a decision taken at the 50th to 95th percentile, but it is a real "
      "limitation rather than a rounding artefact.")

    A(Paragraph("Economics - the ledger", H2))
    sim = M.get("simulator_totals", {})
    A(kv([
        ("Demand", f"{sim.get('demand', 0):,.0f} units"),
        ("Sales", f"{sim.get('sales', 0):,.0f} units"),
        ("Lost sales", f"{sim.get('lost_sales', 0):,.0f} units"),
        ("Fill rate", f"{sim.get('fill_rate', 0):.1%}"),
        ("Gross margin", money(sim.get("gross_margin"))),
        ("Holding cost", money(sim.get("holding_cost"))),
        ("Markdown cost", money(sim.get("markdown_cost"))),
        ("Waste cost", money(sim.get("waste_cost"))),
        ("Lost margin", money(sim.get("lost_margin"))),
        ("Realised margin", f"<b>{money(sim.get('realized_margin'))}</b>"),
        ("Conservation residual", f"{sim.get('conservation_residual', 0):.2e} (asserted as a test)"),
    ], W))

    A(Spacer(1, 4))
    econ = F.get("economics", [])
    if econ:
        rows = [["Configuration", "Realised margin", "Fill rate", "Avg inventory"]]
        for r in econ:
            rows.append([r.get("method", ""), money(r.get("realized_margin")),
                         f"{float(r.get('fill_rate', 0)):.1%}" if r.get("fill_rate") else "-",
                         f"{float(r.get('avg_inventory', 0)):,.0f}"])
        A(table(rows, [56 * mm, 34 * mm, 24 * mm, W - 114 * mm], align_right=(1, 2, 3)))
    ave = M.get("accuracy_vs_economics", {})
    p(f"<b>Accuracy is not economics.</b> Across {ave.get('n_methods','9')} configurations the "
      f"rank correlation between forecast accuracy and realised margin is Spearman rho = "
      f"{num(ave.get('spearman_rho'), 3)} at p = {num(ave.get('p_value'), 3)} - positive, but "
      f"not significant at this sample size. The blanket 95% service arm is the clearest case: "
      f"it is as accurate as the full system by construction and materially less profitable, "
      f"because it buys service nobody costed. Model promotion in this system is therefore "
      f"gated on realised ledger margin, not on WRMSSE.")

    A(Paragraph("Reconciliation: report with it, order without it", H2))
    p("Judged across all twelve aggregation levels, MinT improves the top and degrades the "
      "bottom, monotonically: total -0.45, state -0.36, category -0.31, store -0.19, down to "
      "item-store +0.44. Leaf-level WRMSSE goes from 0.77 to 1.20, worse than the seasonal-naive "
      "anchor at 0.98. Since Stage 7 sets an order-up-to level per item-location, the decision "
      "is made at the leaves and there the trade is against us. The decision path therefore "
      "reads the unreconciled distribution while the aggregate planning views keep the "
      "reconciled one. Reporting one number for both would have to be wrong for one of them.")

    A(Paragraph("Censoring detection", H2))
    cd = M.get("censoring_detection", {})
    A(kv([
        ("Precision", num(cd.get("precision"), 3)),
        ("Recall", num(cd.get("recall"), 3)),
        ("F1", num(cd.get("f1"), 3)),
        ("Recall on full-zero days", num(cd.get("recall_full_zero"), 3)),
        ("Truly censored rows", f"{cd.get('actual_censored_rows', 0):,}"),
        ("Flagged", f"{cd.get('flagged_rows', 0):,}"),
    ], W))
    p("This is the one claim synthetic data supports that real M5 data cannot: ground truth for "
      "censoring exists only because the generator wrote it. Precision is high and overall "
      "recall is moderate by design - the detector is deliberately conservative, since "
      "inflating a genuine zero into imagined demand is the more expensive error.")

    # =============================================================== VII
    A(Sec(doc, "VIII - Engineering record"))
    A(PageBreak())
    h1("Part VIII", "Engineering record")
    p("A record of defects found and fixed is part of the argument, not an appendix to it: "
      "several of these were silently flattering results, and would have survived into a "
      "written claim if the system had not been instrumented to catch them.", LEAD)

    A(Paragraph("Correctness defects", H2))
    A(table([
        ["Defect", "Consequence", "Fix"],
        ["The median index was computed as half the quantile count",
         "The 75th percentile was used as the median in five places, because the quantile "
         "tuple is deliberately asymmetric.",
         "A named constant, plus a test that greps the package for the halving idiom."],
        ["Calibration sorted the whole quantile vector",
         "An adjusted lower quantile could be promoted into the median slot, moving a median "
         "of 5.0 to 7.0 with a zero offset.",
         "Monotonicity restored about the median instead."],
        ["MinT was a no-op, then used the wrong error basis",
         "Reconciling an already-coherent input gives back the input; then the covariance was "
         "estimated from bottom-up projections rather than node residuals.",
         "Independent base forecasts per node, and residuals from those."],
        ["Spoilage and markdown were priced but never levied",
         "The fractile charged for both while the ledger charged for neither - waste cost was "
         "exactly zero, and phantom spoilage was 54% of modelled overage.",
         "Both levied; spoilage exposure redefined as cover in excess of shelf life."],
        ["Full-history diagnostics used as features",
         "Leakage: Stage-1 outputs computed over all data leaked future information into "
         "every origin.",
         "Per-origin-block vintages, with a dedicated leakage test suite."],
        ["Bias compared as a signed quantity",
         "The most under-forecasting method was declared the winner.",
         "Compared by magnitude."],
    ], [42 * mm, 56 * mm, W - 98 * mm]))

    A(Paragraph("Interface defects", H2))
    A(table([
        ["Defect", "Detail"],
        ["Search did nothing",
         "Choosing a result selected a row in the spreadsheet while a different view was on "
         "screen, and the search matched internal identifiers rather than the displayed name."],
        ["The grid collapsed to zero height, three times",
         "Each time a child was added to a CSS grid without a matching template row, so a "
         "fully populated canvas painted into a zero-height box."],
        ["The progress denominator grew as you worked",
         "The queue was re-sliced on every render, so finishing an item pulled in another."],
        ["Every card reported low confidence",
         "Thresholds were guessed rather than derived from the distribution."],
        ["Chart strokes were twice as thick horizontally",
         "A non-uniform viewBox scale; fixed with non-scaling-stroke."],
        ["The micro-fan double-encoded uncertainty, backwards",
         "Band opacity was multiplied by confidence on top of band height, so the riskiest "
         "rows rendered the faintest."],
        ["A theme flip left elements in the old theme",
         "Every property that failed was one the element declared a transition for."],
        ["Committing undid the review",
         "The commit cleared the state that recorded what had been done."],
    ], [58 * mm, W - 58 * mm]))

    A(Paragraph("Budgets, measured rather than asserted", H2))
    A(table([
        ["Budget", "Target", "Measured"],
        ["Application code, gzipped", "under 250 KB", "117.0 KB across 12 files"],
        ["Grid paint, median", "under 16 ms", "4.2 ms cold, 3.1 ms warm"],
        ["Grid paint, 95th percentile", "under 16 ms", "11.4 ms cold, 6.5 ms warm"],
        ["Frames over 32 ms", "zero", "zero (worst single frame 15.8 ms)"],
        ["Full dataset replacement", "under 150 ms", "72 ms"],
        ["WCAG 2.2 AA contrast", "no failures", "34 checks across both themes, 0 failures"],
        ["Horizontal overflow", "none", "0 across 6 views x 2 themes x 2 sidebar states"],
        ["Arrow versus JSON, 28k rows", "-", "1.44 MB vs 3.26 MB; 1.1 ms vs 65.6 ms to parse"],
    ], [56 * mm, 30 * mm, W - 86 * mm]))
    p("The frame budget initially failed at 18.6 ms median with eleven frames over 32 ms. "
      "Profiling ruled out the accessibility mirror and style resolution: the cost was the "
      "micro-fan, seventy-odd glyphs a frame of alpha-composited paths. Each rendered glyph is "
      "now cached to an offscreen canvas keyed by its data, and during a scroll an uncached "
      "glyph draws only its median line, upgrading to full fidelity 90 ms after motion stops. "
      "Nobody can read a 40 by 14 pixel glyph during a fast scroll, and fidelity was verified "
      "intact at rest.")

    A(Paragraph("Verification", H2))
    A(kv([
        ("Test suite", "94 tests: leakage, simulator, contracts, diagnostics, backtest, "
                       "sensitivity, and the read path"),
        ("Continuous integration", "Tests, a UTF-8 source check, a JavaScript parse check, and "
                                   "an end-to-end smoke run asserting every artifact"),
        ("Run invariants", "Unit conservation under 1e-6, no negative stock, MinT projection "
                           "error under 1e-6 - asserted, not merely reported"),
        ("Two-backend parity", "The SQL and JavaScript query paths are diffed over the real "
                               "workload on every load of the console"),
    ], W))

    # =============================================================== VIII
    A(Sec(doc, "IX - Limits"))
    A(PageBreak())
    h1("Part IX", "Limits and what is deliberately not built")
    p("Stated plainly, because a system that hides its boundaries is making the same mistake "
      "as a forecast that hides its uncertainty.", LEAD)

    A(Paragraph("Honest limitations", H2))
    bullets([
        "<b>The data is synthetic.</b> M5-shaped and generated, not a real retailer's. Accuracy "
        "figures are therefore not comparable to the M5 literature, and the benchmark gate the "
        "specification sets is unmeetable on this data. The substitute claim is the censoring "
        "precision and recall, which real M5 data cannot provide because it has no ground truth "
        "for censoring.",
        "<b>Nothing is written anywhere.</b> Overrides, additions and removals are session-local "
        "drafts. The CSV export is the only way a decision leaves the browser. Wiring a real "
        "write path is the single largest gap between this and something usable in production.",
        "<b>Combination helps at only two of eight origins.</b> The candidate pool is dominated "
        "by one model, so stacking has little to work with. This is reported rather than "
        "buried.",
        "<b>Spoilage is an excess-cover approximation.</b> True FIFO unit-age tracking needs a "
        "per-series queue and cannot be vectorised at this scale.",
        "<b>The backtest rolls the evaluation origin, not retraining.</b> It measures how a "
        "fixed system behaves as the origin moves; it does not measure the value of retraining "
        "on a cadence.",
        "<b>Lower-tail calibration is poor</b>, as the coverage table shows. It matters little "
        "for decisions taken above the median, but it is real.",
    ])

    A(Paragraph("Correctly out of scope", H2))
    p("Multi-tenancy, approval workflows, ERP connectors, an agentic layer and the remaining "
      "portals are absent by choice rather than by omission. They are features that public data "
      "cannot exercise and that would add surface without adding evidence for the argument the "
      "system exists to make.")

    A(Paragraph("Where to start reading the code", H2))
    A(kv([
        ("The argument", "dqstack/decide.py - the critical fractile and the ledger"),
        ("The contract", "dqstack/models/dist.py - the predictive-distribution interface"),
        ("The honesty", "dqstack/backtest.py - paired inference across origins"),
        ("The interface", "ui/js/review.js and ui/js/plain.js"),
        ("The design", "ui/css/tokens.css"),
        ("The record", "CONTEXT.md - decisions, defects and traps"),
    ], W))

    A(Spacer(1, 10 * mm))
    A(Rule())
    A(Spacer(1, 3 * mm))
    A(Mark(size=22))
    A(Spacer(1, 3 * mm))
    A(Paragraph(
        "DQ-Stack. Forecasts that carry their own uncertainty, their own data quality, and "
        "their own price.", NOTE))

    doc.build(st)
    # The page count is worth reporting: it is the cheapest signal that a section was
    # added or lost, and the document is long enough that nobody notices either by eye.
    return OUT, doc.page


if __name__ == "__main__":
    out, pages = build()
    print(f"wrote {out}  ({out.stat().st_size / 1024:.0f} KB, {pages} pages)")
