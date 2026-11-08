"""Running an import: match columns, check every row, apply, undo.

The check is a dry run that classifies each good row as new, changed or unchanged
and lists each bad row with its spreadsheet row number and every problem in it.
Committing re-runs the check inside the same transaction that applies it, so what
is written is exactly what the data says at that moment, not what it said when the
person first looked at the report.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import ColumnMapping, ImportChange, ImportFile, ImportJob
from . import parsing as P
from .kinds import KINDS, Kind, Recorder

KEEP = 2000            # errors and warnings stored on the job; the error file has them all


def kind_of(key: str) -> Kind:
    k = KINDS.get(key)
    if k is None:
        raise HTTPException(404, "That kind of import does not exist.")
    return k


def signature(headers: list[str]) -> str:
    """Identifies a source by its column names, in any order, so the matching chosen
    for one file is offered again for the next file from the same system."""
    canon = "|".join(sorted(P.norm(h) for h in headers if h.strip()))
    return hashlib.sha256(canon.encode()).hexdigest()[:32]


def auto_map(kind: Kind, headers: list[str]) -> dict[str, str]:
    """Template column key -> file header, for every column we can recognise."""
    normed = {P.norm(h): h for h in headers if h.strip()}
    out: dict[str, str] = {}
    used: set[str] = set()
    for col in kind.columns:
        for cand in (col.title, col.key, *col.aliases):
            h = normed.get(P.norm(cand))
            if h and h not in used:
                out[col.key] = h
                used.add(h)
                break
    return out


def mapping_problems(kind: Kind, headers: list[str], mapping: dict[str, str]) -> list[str]:
    problems = []
    known = {c.key for c in kind.columns}
    for k, h in mapping.items():
        if k not in known:
            problems.append(f"'{k}' is not a column of the {kind.title.lower()} template")
        elif h not in headers:
            problems.append(f"the file has no column called '{h}'")
    seen: dict[str, str] = {}
    for k, h in mapping.items():
        if h in seen:
            problems.append(f"column '{h}' is matched to two fields")
        seen[h] = k
    for col in kind.columns:
        if col.required and col.key not in mapping:
            problems.append(f"no column is matched to {col.title}, which is required")
    return problems


@dataclass
class Checked:
    items: list[tuple[int, dict, str]] = field(default_factory=list)   # row, clean, new|update|same
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    blank: int = 0
    rows_read: int = 0
    good_rows: int = 0
    state: dict = field(default_factory=dict)

    def count(self, what: str) -> int:
        return sum(1 for _, _, s in self.items if s == what)


def load_file(db: Session, job: ImportJob) -> tuple[list[str], list[list[str]]]:
    f = db.get(ImportFile, job.id)
    if f is None:
        raise HTTPException(410, "The file for this import is no longer stored.")
    return P.read_table(f.content, job.filename or "")


def check(db: Session, c, job: ImportJob) -> Checked:
    kind = kind_of(job.kind)
    headers, rows = load_file(db, job)
    idx = {h: i for i, h in enumerate(headers)}
    cols = {k: idx[h] for k, h in job.mapping.items() if h in idx}
    out = Checked(state=kind.begin(db, c))
    for i, row in enumerate(rows):
        n = i + 2                                     # spreadsheet row; row 1 is the header
        if not any(cell.strip() for cell in row):
            out.blank += 1
            continue
        out.rows_read += 1
        values = {k: row[j] for k, j in cols.items()}
        clean, errors, warnings = kind.check_row(values, n, out.state)
        for w in warnings:
            out.warnings.append({"row": n, "problem": w})
        if errors:
            out.errors.append({"row": n, "problems": errors})
            continue
        out.good_rows += 1
        if hasattr(kind, "aggregate"):
            out.items.append((n, clean, ""))
        else:
            out.items.append((n, clean, kind.classify(clean, out.state)))
    if hasattr(kind, "aggregate"):
        # Kinds whose rows add up (bill-level sales) classify the totals, not the rows.
        out.items, extra_errors = kind.aggregate(db, out.items, out.state)
        out.errors.extend(extra_errors)
        out.errors.sort(key=lambda e: e["row"])
    return out


def _store_summary(job: ImportJob, res: Checked) -> None:
    job.rows_read = res.rows_read
    job.rows_new = res.count("new")
    job.rows_updated = res.count("update")
    job.rows_unchanged = res.count("same")
    job.rows_accepted = res.good_rows
    job.rows_failed = len(res.errors)
    job.errors = res.errors[:KEEP]
    job.warnings = res.warnings[:KEEP]


def run_check(db: Session, c, job: ImportJob) -> ImportJob:
    problems = mapping_problems(kind_of(job.kind), job.headers, job.mapping)
    if problems:
        job.status = "needs_mapping"
        job.message = " ".join(p[0].upper() + p[1:] + "." for p in problems)
        return job
    res = check(db, c, job)
    _store_summary(job, res)
    job.status = "checked"
    job.message = None
    return job


def remember_mapping(db: Session, company_id: str, job: ImportJob) -> None:
    m = db.scalar(select(ColumnMapping).where(ColumnMapping.company_id == company_id,
                                              ColumnMapping.kind == job.kind,
                                              ColumnMapping.signature == job.signature))
    if m is None:
        db.add(ColumnMapping(company_id=company_id, kind=job.kind, signature=job.signature,
                             mapping=dict(job.mapping)))
    else:
        m.mapping = dict(job.mapping)


def remembered(db: Session, company_id: str, kind: str, sig: str) -> dict | None:
    m = db.scalar(select(ColumnMapping).where(ColumnMapping.company_id == company_id,
                                              ColumnMapping.kind == kind,
                                              ColumnMapping.signature == sig))
    return dict(m.mapping) if m else None


def commit(db: Session, c, job: ImportJob) -> ImportJob:
    if job.status not in ("checked",):
        raise HTTPException(409, "Check the file before importing it." if job.status in
                            ("uploaded", "needs_mapping") else
                            f"This import is already {job.status}.")
    kind = kind_of(job.kind)
    res = check(db, c, job)
    _store_summary(job, res)
    rec = Recorder(db, c.company_id, job.id)
    todo = [clean for _, clean, s in res.items if s != "same"]
    kind.apply(db, c, todo, res.state, rec)
    rec.flush()
    job.status = "imported"
    job.imported_at = utcnow()
    remember_mapping(db, c.company_id, job)
    return job


def undo(db: Session, c, job: ImportJob) -> ImportJob:
    if job.status != "imported":
        raise HTTPException(409, "Only a completed import can be undone." if job.status != "undone"
                            else "This import was already undone.")
    kind = kind_of(job.kind)
    changes = list(db.scalars(select(ImportChange).where(ImportChange.job_id == job.id)
                              .order_by(ImportChange.seq.desc())))
    reverted, kept = kind.undo_batch(db, changes, c)
    db.flush()
    job.status = "undone"
    job.undone_at = utcnow()
    job.undo_report = {"reverted": reverted, "kept": len(kept), "reasons": kept[:KEEP]}
    return job


def errors_csv(db: Session, c, job: ImportJob) -> str:
    """Every bad row, as it was in the file, with a Problem column: fix and re-upload."""
    headers, rows = load_file(db, job)
    res = check(db, c, job)
    bad = {e["row"]: "; ".join(e["problems"]) for e in res.errors}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Row", *headers, "Problem"])
    for i, row in enumerate(rows):
        n = i + 2
        if n in bad:
            w.writerow([n, *row, bad[n]])
    return buf.getvalue()


def template(kind: Kind, fmt: str) -> tuple[bytes, str]:
    titles = [c.title for c in kind.columns]
    if fmt == "csv":
        buf = io.StringIO()
        csv.writer(buf).writerow(titles)
        return ("﻿" + buf.getvalue()).encode("utf-8"), "text/csv"
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = kind.title[:31]
    ws.append(titles)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="EDE9FE")
    for i, col in enumerate(kind.columns, start=1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = max(14, len(col.title) + 4)
    ins = wb.create_sheet("Instructions")
    ins.append(["Column", "Required", "What to put", "Example"])
    for cell in ins[1]:
        cell.font = Font(bold=True)
    for col in kind.columns:
        ins.append([col.title, "yes" if col.required else "", col.help, col.example])
    ins.append([])
    ins.append([f"Each row is matched on its {kind.natural_key}. Uploading the same file "
                "again changes nothing; a blank cell keeps what is already stored."])
    for letter, width in zip("ABCD", (24, 10, 60, 24)):
        ins.column_dimensions[letter].width = width
    ex = wb.create_sheet("Example")
    ex.append(titles)
    ex.append([c.example for c in kind.columns])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
