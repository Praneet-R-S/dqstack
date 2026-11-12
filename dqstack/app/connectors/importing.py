"""Connectors bring data in through the Imports pipeline, never around it.

A Tally file or a Zoho sync becomes an ordinary import job of an existing kind
(products, sales history, daily sales), with the rows written in that kind's own
template columns. So it is checked by the same rules as a spreadsheet, a repeat
changes nothing (each kind matches on its natural key and skips identical rows), it
shows on the Imports screen with its problem list, and Undo puts back exactly what
was there before. The converted rows are stored as an .xlsx workbook, which the
pipeline reads cell by cell, rather than CSV, whose delimiter it has to guess.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

from sqlalchemy.orm import Session

from ..imports import engine as E
from ..models import ImportFile, ImportJob


def system_ctx(company_id: str, name: str, device: str) -> SimpleNamespace:
    """Who acts when a scheduled sync imports: the connector, for its own company, with
    an Admin's reach (every store). Like the POS key's context in routes/pos.py."""
    return SimpleNamespace(company_id=company_id, role="admin", all_stores=True,
                           store_ids=set(), person=SimpleNamespace(id=None, name=name),
                           device=device, ip=None, can=lambda *a, **kw: True)


def workbook(headers: list[str], rows: list[list]) -> bytes:
    from openpyxl import Workbook
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Data")
    ws.append(headers)
    for r in rows:
        ws.append(["" if v is None else str(v) for v in r])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def make_job(db: Session, c, kind: str, filename: str, headers: list[str],
             rows: list[list], person_id: str | None = None) -> ImportJob:
    """An import job for converted rows, checked and ready to commit."""
    k = E.kind_of(kind)
    content = workbook(headers, rows)
    job = ImportJob(company_id=c.company_id, kind=kind, filename=filename[:300],
                    signature=E.signature(headers), headers=headers,
                    mapping=E.auto_map(k, headers), person_id=person_id, status="uploaded")
    db.add(job)
    db.flush()
    db.add(ImportFile(job_id=job.id, company_id=c.company_id, size=len(content), content=content))
    db.flush()
    E.run_check(db, c, job)
    return job


def counts(job: ImportJob) -> dict:
    return {"job_id": job.id, "kind": job.kind, "status": job.status, "rows": job.rows_read,
            "new": job.rows_new, "updated": job.rows_updated, "same": job.rows_unchanged,
            "failed": job.rows_failed}
