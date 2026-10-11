"""Pydantic models: the data contracts between the LLM, the pipeline and the API."""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DocumentType(str, Enum):
    INVOICE = "invoice"
    CREDIT_NOTE = "credit_note"
    DELIVERY_NOTE = "delivery_note"
    OTHER = "other"


class LineItemData(BaseModel):
    description: str = ""
    quantity: float | None = None
    unit_price: float | None = None
    line_total: float | None = None
    vat_rate: float | None = None


class ExtractedInvoice(BaseModel):
    """The normalised result of extraction, whatever the source (XML parser or LLM)."""

    document_type: DocumentType = DocumentType.INVOICE
    supplier_name: str | None = None
    supplier_vat_id: str | None = None
    buyer_name: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    po_number: str | None = None
    currency: str | None = "EUR"
    net_total: float | None = None
    vat_total: float | None = None
    gross_total: float | None = None
    line_items: list[LineItemData] = Field(default_factory=list)


# --- What the LLM must return ----------------------------------------------------------
# Separate from ExtractedInvoice on purpose: Gemini's structured-output schema does not
# accept default values, and dates come back as text that we parse ourselves.


class LLMLineItem(BaseModel):
    description: str
    quantity: float | None
    unit_price: float | None
    line_total: float | None
    vat_rate: float | None


class LLMInvoice(BaseModel):
    document_type: DocumentType
    supplier_name: str | None
    supplier_vat_id: str | None
    buyer_name: str | None
    invoice_number: str | None
    invoice_date: str | None
    due_date: str | None
    po_number: str | None
    currency: str | None
    net_total: float | None
    vat_total: float | None
    gross_total: float | None
    line_items: list[LLMLineItem]


# --- API responses and requests ---------------------------------------------------------


class IssueOut(BaseModel):
    code: str
    severity: str
    message: str
    field: str | None = None


class LineItemOut(LineItemData):
    model_config = ConfigDict(from_attributes=True)

    position: int
    description: str | None = ""


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    timestamp: datetime
    actor: str
    action: str
    details: dict[str, Any] = Field(default_factory=dict)


class InvoiceSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    original_filename: str
    source: str
    extraction_method: str
    document_type: str
    status: str
    supplier_name: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    gross_total: float | None = None
    currency: str | None = None
    issues: list[IssueOut] = Field(default_factory=list)
    erp_reference: str | None = None
    created_at: datetime


class InvoiceDetail(InvoiceSummary):
    supplier_vat_id: str | None = None
    buyer_name: str | None = None
    due_date: date | None = None
    po_number: str | None = None
    net_total: float | None = None
    vat_total: float | None = None
    processing_ms: int | None = None
    reviewed_at: datetime | None = None
    line_items: list[LineItemOut] = Field(default_factory=list)
    audit_entries: list[AuditOut] = Field(default_factory=list)


class UploadResult(BaseModel):
    filename: str
    invoice_id: int | None = None
    status: str | None = None
    duplicate_upload: bool = False
    error: str | None = None


class ApproveRequest(BaseModel):
    corrections: ExtractedInvoice | None = None
    reviewer: str = "reviewer"
    note: str | None = None


class RejectRequest(BaseModel):
    reason: str = Field(min_length=3)
    reviewer: str = "reviewer"


class StatsOut(BaseModel):
    total: int
    awaiting_review: int
    auto_approved: int
    auto_approval_rate: float
    by_status: dict[str, int]
    by_method: dict[str, int]
    top_issues: list[dict[str, Any]]
    avg_processing_ms: float | None = None
