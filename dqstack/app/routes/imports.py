"""The Imports screen: every file ever uploaded, and the upload flow itself.

    GET  /api/imports/kinds                     what can be uploaded, and the columns
    GET  /api/imports/templates/{kind}.csv|xlsx a template to fill in
    POST /api/imports                           upload (multipart: kind, file)
    PUT  /api/imports/{id}/mapping              change which column is which
    POST /api/imports/{id}/commit               import the good rows
    POST /api/imports/{id}/undo                 put everything back
    GET  /api/imports/{id}/errors.csv           the bad rows, to fix and upload again
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import ctx
from ..imports import engine as E
from ..imports import parsing as P
from ..imports.kinds import KINDS
from ..models import ImportFile, ImportJob, Person
from ..scope import Ctx, get_or_404

router = APIRouter(prefix="/api/imports", tags=["imports"])


def job_dict(j: ImportJob, names: dict | None = None) -> dict:
    return {"id": j.id, "kind": j.kind, "filename": j.filename, "status": j.status,
            "message": j.message, "headers": j.headers, "mapping": j.mapping,
            "rows_read": j.rows_read, "rows_accepted": j.rows_accepted,
            "rows_new": j.rows_new, "rows_updated": j.rows_updated,
            "rows_unchanged": j.rows_unchanged, "rows_failed": j.rows_failed,
            "errors": j.errors, "warnings": j.warnings,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "imported_at": j.imported_at.isoformat() if j.imported_at else None,
            "undone_at": j.undone_at.isoformat() if j.undone_at else None,
            "undo_report": j.undo_report, "store_id": j.store_id,
            "person": (names or {}).get(j.person_id)}


@router.get("/kinds")
def kinds(c: Ctx = Depends(ctx)):
    return [{"key": k.key, "title": k.title, "natural_key": k.natural_key,
             "columns": [{"key": col.key, "title": col.title, "required": col.required,
                          "help": col.help, "example": col.example} for col in k.columns],
             "templates": {"csv": f"/api/imports/templates/{k.key}.csv",
                           "xlsx": f"/api/imports/templates/{k.key}.xlsx"}}
            for k in KINDS.values()]


@router.get("/templates/{name}")
def template(name: str, c: Ctx = Depends(ctx)):
    key, _, fmt = name.rpartition(".")
    if fmt not in ("csv", "xlsx"):
        raise HTTPException(404, "Templates come as .csv or .xlsx.")
    body, media = E.template(E.kind_of(key), fmt)
    return Response(body, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="dqstack-{key}-template.{fmt}"'})


@router.get("")
def list_jobs(c: Ctx = Depends(ctx), db: Session = Depends(get_db),
              limit: int = Query(100, ge=1, le=500)):
    q = (select(ImportJob).where(ImportJob.company_id == c.company_id)
         .order_by(ImportJob.created_at.desc()).limit(limit))
    if not c.can("company.settings"):
        q = q.where(ImportJob.person_id == c.person.id)
    names = {p.id: p.name for p in db.scalars(select(Person).where(
        Person.company_id == c.company_id))}
    out = []
    for j in db.scalars(q):
        d = job_dict(j, names)
        d.pop("errors"), d.pop("warnings"), d.pop("headers")
        out.append(d)
    return out


def _job(db: Session, c: Ctx, job_id: str) -> ImportJob:
    j = get_or_404(db, c, ImportJob, job_id, store_attr=None, what="That import")
    if j.person_id != c.person.id and not c.can("company.settings"):
        raise HTTPException(404, "That import was not found.")
    return j


@router.get("/{job_id}")
def get_job(job_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    return job_dict(_job(db, c, job_id))


@router.post("", status_code=201)
async def upload(kind: str = Form(...), file: UploadFile = File(...),
                 c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    k = E.kind_of(kind)
    k.authorise(c)
    content = await file.read(P.MAX_BYTES + 1)
    try:
        headers, rows = P.read_table(content, file.filename or "")
    except P.FileError as e:
        raise HTTPException(422, str(e)) from None
    sig = E.signature(headers)
    mapping = E.remembered(db, c.company_id, kind, sig) or E.auto_map(k, headers)
    job = ImportJob(company_id=c.company_id, kind=kind, filename=(file.filename or "")[:300],
                    signature=sig, headers=headers, mapping=mapping, person_id=c.person.id,
                    status="uploaded")
    db.add(job)
    db.flush()
    db.add(ImportFile(job_id=job.id, company_id=c.company_id, size=len(content), content=content))
    db.flush()
    E.run_check(db, c, job)
    audit.log(db, c, "import.uploaded", "import", job.id,
              after={"kind": kind, "filename": job.filename, "rows": len(rows)})
    db.commit()
    return job_dict(job)


class MappingIn(BaseModel):
    mapping: dict[str, str]


@router.put("/{job_id}/mapping")
def set_mapping(job_id: str, body: MappingIn, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    job = _job(db, c, job_id)
    E.kind_of(job.kind).authorise(c)
    if job.status not in ("uploaded", "needs_mapping", "checked"):
        raise HTTPException(409, f"This import is already {job.status}.")
    job.mapping = {k: v for k, v in body.mapping.items() if v}
    E.run_check(db, c, job)
    db.commit()
    return job_dict(job)


@router.post("/{job_id}/commit")
def commit(job_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    job = _job(db, c, job_id)
    E.kind_of(job.kind).authorise(c)
    E.commit(db, c, job)
    audit.log(db, c, "import.committed", "import", job.id, after={
        "kind": job.kind, "new": job.rows_new, "updated": job.rows_updated,
        "unchanged": job.rows_unchanged, "failed": job.rows_failed})
    db.commit()
    return job_dict(job)


@router.post("/{job_id}/undo")
def undo(job_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    job = _job(db, c, job_id)
    E.kind_of(job.kind).authorise(c)
    E.undo(db, c, job)
    audit.log(db, c, "import.undone", "import", job.id, after=job.undo_report)
    db.commit()
    return job_dict(job)


@router.get("/{job_id}/errors.csv")
def errors_file(job_id: str, c: Ctx = Depends(ctx), db: Session = Depends(get_db)):
    job = _job(db, c, job_id)
    body = E.errors_csv(db, c, job)
    stem = (job.filename or "import").rsplit(".", 1)[0]
    return Response("\ufeff" + body, media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{stem}-problems.csv"'})
