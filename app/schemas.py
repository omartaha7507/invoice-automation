"""Pydantic models: the data contracts between the LLM, the pipeline and the API."""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class DocumentType(str, Enum):
    INVOICE = "invoice"
    CREDIT_NOTE = "credit_note"
    DELIVERY_NOTE = "delivery_note"
    OTHER = "other"


class LineItemData(BaseModel):
    description: str = ""
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    line_total: Optional[float] = None
    vat_rate: Optional[float] = None


class ExtractedInvoice(BaseModel):
    """The normalised result of extraction, whatever the source (XML parser or LLM)."""

    document_type: DocumentType = DocumentType.INVOICE
    supplier_name: Optional[str] = None
    supplier_vat_id: Optional[str] = None
    buyer_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    po_number: Optional[str] = None
    currency: Optional[str] = "EUR"
    net_total: Optional[float] = None
    vat_total: Optional[float] = None
    gross_total: Optional[float] = None
    line_items: list[LineItemData] = Field(default_factory=list)


# --- What the LLM must return ----------------------------------------------------------
# Separate from ExtractedInvoice on purpose: Gemini's structured-output schema does not
# accept default values, and dates come back as text that we parse ourselves.


class LLMLineItem(BaseModel):
    description: str
    quantity: Optional[float]
    unit_price: Optional[float]
    line_total: Optional[float]
    vat_rate: Optional[float]


class LLMInvoice(BaseModel):
    document_type: DocumentType
    supplier_name: Optional[str]
    supplier_vat_id: Optional[str]
    buyer_name: Optional[str]
    invoice_number: Optional[str]
    invoice_date: Optional[str]
    due_date: Optional[str]
    po_number: Optional[str]
    currency: Optional[str]
    net_total: Optional[float]
    vat_total: Optional[float]
    gross_total: Optional[float]
    line_items: list[LLMLineItem]


# --- API responses and requests ---------------------------------------------------------


class IssueOut(BaseModel):
    code: str
    severity: str
    message: str
    field: Optional[str] = None


class LineItemOut(LineItemData):
    model_config = ConfigDict(from_attributes=True)

    position: int
    description: Optional[str] = ""


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
    supplier_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    gross_total: Optional[float] = None
    currency: Optional[str] = None
    issues: list[IssueOut] = Field(default_factory=list)
    erp_reference: Optional[str] = None
    created_at: datetime


class InvoiceDetail(InvoiceSummary):
    supplier_vat_id: Optional[str] = None
    buyer_name: Optional[str] = None
    due_date: Optional[date] = None
    po_number: Optional[str] = None
    net_total: Optional[float] = None
    vat_total: Optional[float] = None
    processing_ms: Optional[int] = None
    reviewed_at: Optional[datetime] = None
    line_items: list[LineItemOut] = Field(default_factory=list)
    audit_entries: list[AuditOut] = Field(default_factory=list)


class UploadResult(BaseModel):
    filename: str
    invoice_id: Optional[int] = None
    status: Optional[str] = None
    duplicate_upload: bool = False
    error: Optional[str] = None


class ApproveRequest(BaseModel):
    corrections: Optional[ExtractedInvoice] = None
    reviewer: str = "reviewer"
    note: Optional[str] = None


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
    avg_processing_ms: Optional[float] = None
