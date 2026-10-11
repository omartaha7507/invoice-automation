"""Database tables.

invoices      one row per received document
line_items    many rows per invoice (one-to-many via invoice_id)
purchase_orders  reference data the invoices are checked against
audit_log     every automatic and human action, for traceability (GoBD-style)
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.constants import STATUS_NEEDS_REVIEW
from app.database import Base

# Money is stored as an exact decimal (never a float column) but handed to Python as float.
Money = Numeric(12, 2, asdecimal=False)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)

    # The source document
    original_filename: Mapped[str] = mapped_column(String(255))
    file_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    stored_path: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(20), default="upload")
    extraction_method: Mapped[str] = mapped_column(String(20))

    # Extracted data
    document_type: Mapped[str] = mapped_column(String(20), default="invoice")
    supplier_name: Mapped[Optional[str]] = mapped_column(String(255))
    supplier_vat_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    buyer_name: Mapped[Optional[str]] = mapped_column(String(255))
    invoice_number: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    invoice_date: Mapped[Optional[date]] = mapped_column(Date)
    due_date: Mapped[Optional[date]] = mapped_column(Date)
    po_number: Mapped[Optional[str]] = mapped_column(String(100))
    currency: Mapped[Optional[str]] = mapped_column(String(3))
    net_total: Mapped[Optional[float]] = mapped_column(Money)
    vat_total: Mapped[Optional[float]] = mapped_column(Money)
    gross_total: Mapped[Optional[float]] = mapped_column(Money)

    # Workflow
    status: Mapped[str] = mapped_column(String(20), default=STATUS_NEEDS_REVIEW, index=True)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    erp_reference: Mapped[Optional[str]] = mapped_column(String(100))
    processing_ms: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    line_items: Mapped[list["LineItem"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="LineItem.position"
    )
    audit_entries: Mapped[list["AuditLog"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="AuditLog.id"
    )


class LineItem(Base):
    __tablename__ = "line_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    description: Mapped[Optional[str]] = mapped_column(Text)
    quantity: Mapped[Optional[float]] = mapped_column(Numeric(12, 3, asdecimal=False))
    unit_price: Mapped[Optional[float]] = mapped_column(Money)
    line_total: Mapped[Optional[float]] = mapped_column(Money)
    vat_rate: Mapped[Optional[float]] = mapped_column(Numeric(5, 2, asdecimal=False))

    invoice: Mapped[Invoice] = relationship(back_populates="line_items")


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    po_number: Mapped[str] = mapped_column(String(100), primary_key=True)
    supplier_name: Mapped[str] = mapped_column(String(255))
    amount: Mapped[float] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(50))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    invoice: Mapped[Invoice] = relationship(back_populates="audit_entries")
