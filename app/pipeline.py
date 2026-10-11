"""The heart of the system: document in -> data extracted -> checked -> routed -> exported.

    receive file ─► extract ─► validate ─► decide ─┬─► approved ─► send to accounting (ERP)
                                                   └─► needs_review ─► human approves/rejects
"""
from __future__ import annotations

import csv
import hashlib
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.constants import (
    METHOD_FAILED,
    METHOD_LLM_TEXT,
    METHOD_LLM_VISION,
    METHOD_XML,
    METHOD_XML_EMBEDDED,
    STATUS_APPROVED,
    STATUS_NEEDS_REVIEW,
    STATUS_REJECTED,
    SUPPORTED_TYPES,
)
from app.erp import ERPClient, ERPError
from app.extraction.base import Extractor
from app.extraction.pdf import find_embedded_einvoice, pdf_text
from app.extraction.xml_einvoice import parse_einvoice_xml
from app.models import AuditLog, Invoice, LineItem, PurchaseOrder
from app.redaction import redact_pii
from app.schemas import DocumentType, ExtractedInvoice, LineItemData
from app.validation import ERROR, Issue, PurchaseOrderRef, decide_status, validate_invoice

log = logging.getLogger(__name__)

# A digital PDF has a text layer; a scan has (almost) none and goes to the vision model.
MIN_TEXT_CHARS = 80

HEADER_FIELDS = (
    "document_type",
    "supplier_name",
    "supplier_vat_id",
    "buyer_name",
    "invoice_number",
    "invoice_date",
    "due_date",
    "po_number",
    "currency",
    "net_total",
    "vat_total",
    "gross_total",
)


class UnsupportedFileType(ValueError):
    pass


class InvalidStateError(ValueError):
    pass


# --- Extraction routing -------------------------------------------------------------------


def extract_document(
    data: bytes, filename: str, extractor: Extractor, *, redact: bool = True
) -> tuple[ExtractedInvoice, str]:
    """Pick the cheapest reliable way to read the document. Returns (data, method)."""
    suffix = Path(filename).suffix.lower()

    if suffix == ".xml":
        return parse_einvoice_xml(data), METHOD_XML

    if suffix == ".pdf":
        embedded = find_embedded_einvoice(data)
        if embedded is not None:
            return parse_einvoice_xml(embedded), METHOD_XML_EMBEDDED
        text = pdf_text(data)
        if len(text.strip()) >= MIN_TEXT_CHARS:
            return extractor.extract_from_text(redact_pii(text) if redact else text), METHOD_LLM_TEXT
        return extractor.extract_from_file(data, SUPPORTED_TYPES[suffix]), METHOD_LLM_VISION

    if suffix in SUPPORTED_TYPES:  # images
        return extractor.extract_from_file(data, SUPPORTED_TYPES[suffix]), METHOD_LLM_VISION

    raise UnsupportedFileType(f"{suffix or 'Files without an extension'} is not supported.")


# --- Converting between the database row and the Pydantic model -------------------------


def apply_extracted(invoice: Invoice, extracted: ExtractedInvoice) -> None:
    for field in HEADER_FIELDS:
        value = getattr(extracted, field)
        if isinstance(value, DocumentType):
            value = value.value
        setattr(invoice, field, value)
    invoice.line_items = [
        LineItem(position=index, **item.model_dump()) for index, item in enumerate(extracted.line_items, start=1)
    ]


def invoice_to_extracted(invoice: Invoice) -> ExtractedInvoice:
    values: dict[str, Any] = {field: getattr(invoice, field) for field in HEADER_FIELDS}
    values["document_type"] = DocumentType(invoice.document_type or "invoice")
    values["line_items"] = [
        LineItemData(
            description=item.description or "",
            quantity=item.quantity,
            unit_price=item.unit_price,
            line_total=item.line_total,
            vat_rate=item.vat_rate,
        )
        for item in invoice.line_items
    ]
    return ExtractedInvoice(**values)


def erp_payload(invoice: Invoice) -> dict[str, Any]:
    data = invoice_to_extracted(invoice).model_dump(mode="json")
    data.pop("document_type")
    return {"invoice_id": invoice.id, **data, "source_document_sha256": invoice.file_sha256}


def add_audit(db: Session, invoice: Invoice, actor: str, action: str, details: dict | None = None) -> None:
    db.add(AuditLog(invoice=invoice, actor=actor, action=action, details=details or {}))


def _diff(before: ExtractedInvoice, after: ExtractedInvoice) -> dict[str, dict[str, Any]]:
    old, new = before.model_dump(mode="json"), after.model_dump(mode="json")
    return {key: {"from": old[key], "to": new[key]} for key in new if old.get(key) != new[key]}


# --- The pipeline -------------------------------------------------------------------------


class InvoicePipeline:
    def __init__(
        self,
        extractor: Extractor,
        erp: ERPClient,
        documents_dir: Path,
        *,
        redact_pii: bool = True,
        auto_approve_with_warnings: bool = False,
    ) -> None:
        self.extractor = extractor
        self.erp = erp
        self.documents_dir = Path(documents_dir)
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        self.redact_pii = redact_pii
        self.auto_approve_with_warnings = auto_approve_with_warnings

    # Processing a new document

    def process(self, db: Session, data: bytes, filename: str, source: str = "upload") -> tuple[Invoice, bool]:
        """Process one file. Returns (invoice, is_new); is_new is False for an exact re-upload."""
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_TYPES:
            raise UnsupportedFileType(
                f"{suffix or 'Files without an extension'} is not supported. Use PDF, XML, PNG or JPG."
            )

        sha256 = hashlib.sha256(data).hexdigest()
        existing = db.scalar(select(Invoice).where(Invoice.file_sha256 == sha256))
        if existing is not None:
            add_audit(db, existing, "system", "duplicate_file_ignored", {"filename": filename, "source": source})
            db.commit()
            return existing, False

        # Keep the original: the reviewer compares against it, and bookkeeping rules require it.
        stored_path = self.documents_dir / f"{sha256}{suffix}"
        if not stored_path.exists():
            stored_path.write_bytes(data)

        started = time.perf_counter()
        try:
            extracted, method = extract_document(data, filename, self.extractor, redact=self.redact_pii)
            issues = None
        except Exception as exc:  # never lose a document: a human can still enter it by hand
            log.exception("Extraction failed for %s", filename)
            extracted, method = ExtractedInvoice(), METHOD_FAILED
            issues = [
                Issue("extraction_failed", ERROR, f"Could not read this document automatically: {str(exc)[:300]}")
            ]
        if issues is None:
            issues = self._check(db, extracted)
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        status = decide_status(issues, auto_approve_with_warnings=self.auto_approve_with_warnings)
        invoice = Invoice(
            original_filename=filename[:255],
            file_sha256=sha256,
            stored_path=str(stored_path),
            mime_type=SUPPORTED_TYPES[suffix],
            source=source,
            extraction_method=method,
            status=status,
            issues=[issue.to_dict() for issue in issues],
            processing_ms=elapsed_ms,
        )
        apply_extracted(invoice, extracted)
        db.add(invoice)
        db.flush()  # assigns invoice.id
        add_audit(
            db,
            invoice,
            "system",
            "processed",
            {"method": method, "status": status, "issues": [i.code for i in issues], "processing_ms": elapsed_ms},
        )
        if status == STATUS_APPROVED:
            self._export(db, invoice, actor="system")

        db.commit()
        db.refresh(invoice)
        log.info("Processed %s -> #%s %s via %s in %dms", filename, invoice.id, invoice.status, method, elapsed_ms)
        return invoice, True

    # Human decisions

    def approve(
        self,
        db: Session,
        invoice: Invoice,
        *,
        corrections: ExtractedInvoice | None = None,
        reviewer: str = "reviewer",
        note: str | None = None,
    ) -> Invoice:
        if invoice.status == STATUS_APPROVED and invoice.erp_reference:
            raise InvalidStateError("This invoice is already approved and sent to accounting.")
        if invoice.status == STATUS_REJECTED:
            raise InvalidStateError("This invoice was rejected and can't be approved.")

        if corrections is not None:
            changes = _diff(invoice_to_extracted(invoice), corrections)
            if changes:
                apply_extracted(invoice, corrections)
                add_audit(db, invoice, reviewer, "corrected", {"changes": changes})

        # Re-run the checks on the corrected data. Anything still flagged was knowingly overridden.
        remaining = self._check(db, invoice_to_extracted(invoice), exclude_id=invoice.id)
        invoice.issues = [issue.to_dict() for issue in remaining]
        invoice.status = STATUS_APPROVED
        invoice.reviewed_at = datetime.now(UTC)
        add_audit(db, invoice, reviewer, "approved", {"note": note, "overridden_issues": [i.code for i in remaining]})
        self._export(db, invoice, actor=reviewer)
        db.commit()
        db.refresh(invoice)
        return invoice

    def reject(self, db: Session, invoice: Invoice, *, reason: str, reviewer: str = "reviewer") -> Invoice:
        if invoice.status == STATUS_APPROVED and invoice.erp_reference:
            raise InvalidStateError("Already sent to accounting. Cancel the booking there instead.")
        invoice.status = STATUS_REJECTED
        invoice.reviewed_at = datetime.now(UTC)
        add_audit(db, invoice, reviewer, "rejected", {"reason": reason})
        db.commit()
        db.refresh(invoice)
        return invoice

    # Internals

    def _check(self, db: Session, extracted: ExtractedInvoice, exclude_id: int | None = None) -> list[Issue]:
        return validate_invoice(
            extracted,
            is_duplicate=self._is_duplicate(db, extracted, exclude_id),
            purchase_order=self._find_purchase_order(db, extracted.po_number),
        )

    @staticmethod
    def _is_duplicate(db: Session, extracted: ExtractedInvoice, exclude_id: int | None) -> bool:
        """Same invoice number from the same supplier, not counting rejected ones."""
        if not extracted.invoice_number:
            return False
        same_supplier = []
        if extracted.supplier_vat_id:
            same_supplier.append(Invoice.supplier_vat_id == extracted.supplier_vat_id)
        if extracted.supplier_name:
            same_supplier.append(func.lower(Invoice.supplier_name) == extracted.supplier_name.lower())
        if not same_supplier:
            return False
        query = (
            select(func.count())
            .select_from(Invoice)
            .where(Invoice.invoice_number == extracted.invoice_number)
            .where(Invoice.status != STATUS_REJECTED)
            .where(or_(*same_supplier))
        )
        if exclude_id is not None:
            query = query.where(Invoice.id != exclude_id)
        return (db.scalar(query) or 0) > 0

    @staticmethod
    def _find_purchase_order(db: Session, po_number: str | None) -> PurchaseOrderRef | None:
        if not po_number:
            return None
        po = db.get(PurchaseOrder, po_number.strip())
        if po is None:
            return None
        return PurchaseOrderRef(po.po_number, po.supplier_name, float(po.amount), po.currency)

    def _export(self, db: Session, invoice: Invoice, *, actor: str) -> None:
        try:
            reference = self.erp.push(erp_payload(invoice))
        except ERPError as exc:
            log.error("Export of invoice #%s failed: %s", invoice.id, exc)
            invoice.status = STATUS_NEEDS_REVIEW
            invoice.issues = list(invoice.issues or []) + [
                Issue("erp_export_failed", ERROR, f"Sending to accounting failed: {exc}. Approve again to retry.").to_dict()
            ]
            add_audit(db, invoice, actor, "export_failed", {"error": str(exc)})
            return
        invoice.erp_reference = reference
        add_audit(db, invoice, actor, "exported", {"erp_reference": reference})


def load_purchase_orders(db: Session, csv_path: Path) -> int:
    """Load or update purchase orders from a CSV with columns po_number,supplier_name,amount,currency."""
    count = 0
    with Path(csv_path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            db.merge(
                PurchaseOrder(
                    po_number=row["po_number"].strip(),
                    supplier_name=row["supplier_name"].strip(),
                    amount=float(row["amount"]),
                    currency=(row.get("currency") or "EUR").strip(),
                )
            )
            count += 1
    db.commit()
    return count
