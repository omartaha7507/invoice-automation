"""REST API and review UI.

Run locally:  uvicorn app.api:app --reload
Docs:         http://localhost:8000/docs
"""
from __future__ import annotations

import csv
import io
import logging
from collections import Counter
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.constants import ALL_STATUSES, STATUS_APPROVED, STATUS_NEEDS_REVIEW
from app.database import SessionLocal, get_db, init_db
from app.factory import build_pipeline
from app.models import Invoice
from app.pipeline import InvalidStateError, InvoicePipeline, UnsupportedFileType, load_purchase_orders
from app.schemas import (
    ApproveRequest,
    InvoiceDetail,
    InvoiceSummary,
    RejectRequest,
    StatsOut,
    UploadResult,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("invoice_api")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.ensure_dirs()
    init_db()
    if settings.purchase_orders_csv and Path(settings.purchase_orders_csv).exists():
        with SessionLocal() as db:
            count = load_purchase_orders(db, Path(settings.purchase_orders_csv))
        log.info("Loaded %d purchase orders from %s", count, settings.purchase_orders_csv)
    yield


app = FastAPI(
    title="Invoice Automation Pipeline",
    description="Reads incoming invoices, checks them, and sends clean ones to accounting.",
    version="1.0.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@lru_cache
def get_pipeline() -> InvoicePipeline:
    return build_pipeline(settings)


def _get_invoice(db: Session, invoice_id: int) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} not found")
    return invoice


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(select(1))
    return {"status": "ok"}


@app.post("/api/documents", response_model=list[UploadResult])
def upload_documents(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    pipeline: InvoicePipeline = Depends(get_pipeline),
) -> list[UploadResult]:
    # A plain `def` (not async): FastAPI runs it in a worker thread, so slow LLM calls
    # don't block the server for other users.
    limit = settings.max_upload_mb * 1024 * 1024
    results: list[UploadResult] = []
    for upload in files:
        name = Path(upload.filename or "upload").name
        data = upload.file.read(limit + 1)
        if not data:
            results.append(UploadResult(filename=name, error="The file is empty."))
            continue
        if len(data) > limit:
            results.append(UploadResult(filename=name, error=f"The file is larger than {settings.max_upload_mb} MB."))
            continue
        try:
            invoice, is_new = pipeline.process(db, data, name, source="upload")
        except UnsupportedFileType as exc:
            results.append(UploadResult(filename=name, error=str(exc)))
            continue
        results.append(
            UploadResult(filename=name, invoice_id=invoice.id, status=invoice.status, duplicate_upload=not is_new)
        )
    return results


@app.get("/api/invoices", response_model=list[InvoiceSummary])
def list_invoices(
    status: Optional[str] = Query(None, description="needs_review, approved or rejected"),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[Invoice]:
    query = select(Invoice).order_by(Invoice.created_at.desc(), Invoice.id.desc()).limit(limit)
    if status:
        if status not in ALL_STATUSES:
            raise HTTPException(status_code=422, detail=f"status must be one of {', '.join(ALL_STATUSES)}")
        query = query.where(Invoice.status == status)
    return list(db.scalars(query).all())


@app.get("/api/invoices/{invoice_id}", response_model=InvoiceDetail)
def get_invoice(invoice_id: int, db: Session = Depends(get_db)) -> Invoice:
    return _get_invoice(db, invoice_id)


@app.get("/api/invoices/{invoice_id}/file")
def get_invoice_file(invoice_id: int, db: Session = Depends(get_db)) -> FileResponse:
    invoice = _get_invoice(db, invoice_id)
    path = Path(invoice.stored_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="The original file is missing from storage")
    return FileResponse(
        path, media_type=invoice.mime_type, filename=invoice.original_filename, content_disposition_type="inline"
    )


@app.post("/api/invoices/{invoice_id}/approve", response_model=InvoiceDetail)
def approve_invoice(
    invoice_id: int,
    request: ApproveRequest,
    db: Session = Depends(get_db),
    pipeline: InvoicePipeline = Depends(get_pipeline),
) -> Invoice:
    invoice = _get_invoice(db, invoice_id)
    try:
        return pipeline.approve(
            db, invoice, corrections=request.corrections, reviewer=request.reviewer, note=request.note
        )
    except InvalidStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/invoices/{invoice_id}/reject", response_model=InvoiceDetail)
def reject_invoice(
    invoice_id: int,
    request: RejectRequest,
    db: Session = Depends(get_db),
    pipeline: InvoicePipeline = Depends(get_pipeline),
) -> Invoice:
    invoice = _get_invoice(db, invoice_id)
    try:
        return pipeline.reject(db, invoice, reason=request.reason, reviewer=request.reviewer)
    except InvalidStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/stats", response_model=StatsOut)
def stats(db: Session = Depends(get_db)) -> StatsOut:
    by_status = dict(db.execute(select(Invoice.status, func.count()).group_by(Invoice.status)).all())
    by_method = dict(db.execute(select(Invoice.extraction_method, func.count()).group_by(Invoice.extraction_method)).all())
    total = sum(by_status.values())
    auto_approved = db.scalar(
        select(func.count()).select_from(Invoice).where(Invoice.status == STATUS_APPROVED, Invoice.reviewed_at.is_(None))
    ) or 0
    issue_counter: Counter[str] = Counter()
    for issues in db.scalars(select(Invoice.issues)):
        issue_counter.update(issue["code"] for issue in (issues or []))
    avg_ms = db.scalar(select(func.avg(Invoice.processing_ms)))
    return StatsOut(
        total=total,
        awaiting_review=by_status.get(STATUS_NEEDS_REVIEW, 0),
        auto_approved=auto_approved,
        auto_approval_rate=round(auto_approved / total, 3) if total else 0.0,
        by_status=by_status,
        by_method=by_method,
        top_issues=[{"code": code, "count": count} for code, count in issue_counter.most_common(8)],
        avg_processing_ms=round(float(avg_ms), 1) if avg_ms is not None else None,
    )


def _de_amount(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.2f}".replace(".", ",")


@app.get("/api/export.csv")
def export_csv(status: str = Query(STATUS_APPROVED), db: Session = Depends(get_db)) -> Response:
    """Spreadsheet export in the format German Excel opens correctly (; separator, decimal comma, BOM)."""
    if status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {', '.join(ALL_STATUSES)}")
    invoices = db.scalars(select(Invoice).where(Invoice.status == status).order_by(Invoice.invoice_date, Invoice.id))
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        ["id", "supplier", "vat_id", "invoice_number", "invoice_date", "due_date", "po_number",
         "currency", "net", "vat", "gross", "erp_reference", "file"]
    )
    for inv in invoices:
        writer.writerow(
            [inv.id, inv.supplier_name or "", inv.supplier_vat_id or "", inv.invoice_number or "",
             inv.invoice_date.isoformat() if inv.invoice_date else "",
             inv.due_date.isoformat() if inv.due_date else "", inv.po_number or "", inv.currency or "",
             _de_amount(inv.net_total), _de_amount(inv.vat_total), _de_amount(inv.gross_total),
             inv.erp_reference or "", inv.original_filename]
        )
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="invoices-{status}.csv"'},
    )
