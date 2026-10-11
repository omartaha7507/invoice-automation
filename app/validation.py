"""Deterministic business rules.

The LLM reads the document; these rules decide whether the result can be trusted. Nothing
here touches the database or the network, which keeps the rules easy to unit-test. The
pipeline does the lookups (duplicate? purchase order?) and passes the answers in.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from app.constants import STATUS_APPROVED, STATUS_NEEDS_REVIEW
from app.schemas import DocumentType, ExtractedInvoice

ERROR = "error"      # the data is wrong or risky: a human must look
WARNING = "warning"  # unusual but possibly fine: a human should look (configurable)

STANDARD_DE_VAT_RATES = (0.0, 7.0, 19.0)
SMALL_INVOICE_LIMIT = 250.0  # German "Kleinbetragsrechnung": below this, fewer mandatory fields

REQUIRED_FIELDS = {
    "supplier_name": "Supplier name",
    "invoice_number": "Invoice number",
    "invoice_date": "Invoice date",
    "gross_total": "Total amount",
}

_VAT_PATTERNS = {
    "DE": r"DE\d{9}",
    "AT": r"ATU\d{8}",
    "NL": r"NL\d{9}B\d{2}",
    "FR": r"FR[0-9A-HJ-NP-Z]{2}\d{9}",
    "BE": r"BE[01]\d{9}",
    "IT": r"IT\d{11}",
    "ES": r"ES[0-9A-Z]\d{7}[0-9A-Z]",
    "PL": r"PL\d{10}",
    "DK": r"DK\d{8}",
}
_GENERIC_VAT_PATTERN = r"[A-Z]{2}[0-9A-Z]{2,13}"


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    message: str
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PurchaseOrderRef:
    po_number: str
    supplier_name: str
    amount: float
    currency: str = "EUR"


# --- VAT IDs ------------------------------------------------------------------------------


def normalise_vat_id(value: str) -> str:
    return re.sub(r"[\s.\-]", "", value).upper()


def german_vat_check_digit(first_eight_digits: str) -> int:
    """Check digit of a German USt-IdNr (ISO 7064, MOD 11,10), as used by the tax office."""
    product = 10
    for char in first_eight_digits:
        total = (int(char) + product) % 10
        if total == 0:
            total = 10
        product = (2 * total) % 11
    check = 11 - product
    return 0 if check == 10 else check


def vat_id_problem(value: str) -> str | None:
    """Returns None if the VAT ID looks valid, otherwise "format" or "checksum"."""
    vat = normalise_vat_id(value)
    country = vat[:2]
    pattern = _VAT_PATTERNS.get(country, _GENERIC_VAT_PATTERN)
    if not re.fullmatch(pattern, vat):
        return "format"
    if country == "DE" and german_vat_check_digit(vat[2:10]) != int(vat[10]):
        return "checksum"
    return None


# --- Helpers ------------------------------------------------------------------------------


def _close(a: float, b: float, tolerance: float = 0.02) -> bool:
    return abs(a - b) <= tolerance + 1e-9


def normalise_name(name: str) -> str:
    return re.sub(r"[^0-9a-zäöüß]+", "", name.casefold())


# --- The rules ----------------------------------------------------------------------------


def validate_invoice(
    invoice: ExtractedInvoice,
    *,
    is_duplicate: bool = False,
    purchase_order: PurchaseOrderRef | None = None,
    today: date | None = None,
) -> list[Issue]:
    today = today or date.today()

    if invoice.document_type in (DocumentType.DELIVERY_NOTE, DocumentType.OTHER):
        label = invoice.document_type.value.replace("_", " ")
        return [
            Issue(
                "not_an_invoice",
                ERROR,
                f"This looks like a {label}, not an invoice. Decide whether it needs booking.",
                "document_type",
            )
        ]

    issues: list[Issue] = []

    if invoice.document_type == DocumentType.CREDIT_NOTE:
        issues.append(Issue("credit_note", WARNING, "Credit notes always get a human check.", "document_type"))

    for field, label in REQUIRED_FIELDS.items():
        if getattr(invoice, field) in (None, ""):
            issues.append(Issue("missing_field", ERROR, f"{label} is missing.", field))

    # 1. Do the line items add up to the net total?
    line_totals = [li.line_total for li in invoice.line_items if li.line_total is not None]
    if invoice.net_total is not None and line_totals:
        line_sum = round(sum(line_totals), 2)
        tolerance = max(0.02, 0.01 * len(line_totals))  # allow per-line rounding
        if not _close(line_sum, invoice.net_total, tolerance):
            issues.append(
                Issue(
                    "line_items_sum_mismatch",
                    ERROR,
                    f"Line items add up to {line_sum:.2f}, but the net total says {invoice.net_total:.2f}.",
                    "net_total",
                )
            )

    # 2. Does net + VAT equal the total?
    if None not in (invoice.net_total, invoice.vat_total, invoice.gross_total):
        expected = round(invoice.net_total + invoice.vat_total, 2)
        if not _close(expected, invoice.gross_total):
            issues.append(
                Issue(
                    "totals_mismatch",
                    ERROR,
                    f"Net {invoice.net_total:.2f} + VAT {invoice.vat_total:.2f} = {expected:.2f}, "
                    f"but the total says {invoice.gross_total:.2f}.",
                    "gross_total",
                )
            )

    # 3. Are the VAT rates plausible for a German supplier?
    is_german_supplier = not invoice.supplier_vat_id or normalise_vat_id(invoice.supplier_vat_id).startswith("DE")
    rates = {li.vat_rate for li in invoice.line_items if li.vat_rate is not None}
    unusual = sorted(r for r in rates if not any(_close(r, s, 0.01) for s in STANDARD_DE_VAT_RATES))
    if unusual and is_german_supplier:
        shown = ", ".join(f"{r:g}%" for r in unusual)
        issues.append(
            Issue("unusual_vat_rate", WARNING, f"VAT rate {shown} is not a German rate (0, 7 or 19%).", "line_items")
        )

    # 4. Is the supplier's VAT ID valid?
    if invoice.supplier_vat_id:
        problem = vat_id_problem(invoice.supplier_vat_id)
        if problem == "format":
            issues.append(
                Issue("invalid_vat_id", ERROR, f"VAT ID {invoice.supplier_vat_id} has an invalid format.", "supplier_vat_id")
            )
        elif problem == "checksum":
            issues.append(
                Issue(
                    "invalid_vat_id",
                    ERROR,
                    f"VAT ID {invoice.supplier_vat_id} fails the German check-digit test: a typo or a fake ID.",
                    "supplier_vat_id",
                )
            )
    elif invoice.gross_total is not None and invoice.gross_total > SMALL_INVOICE_LIMIT:
        issues.append(
            Issue(
                "missing_vat_id",
                WARNING,
                "No supplier VAT ID found. Invoices over 250 € must show a tax number or VAT ID.",
                "supplier_vat_id",
            )
        )

    # 5. Have we seen this invoice before?
    if is_duplicate:
        issues.append(
            Issue(
                "duplicate_invoice",
                ERROR,
                f"Invoice {invoice.invoice_number} from {invoice.supplier_name} was already received.",
                "invoice_number",
            )
        )

    # 6. Dates and currency
    if invoice.invoice_date and invoice.invoice_date > today:
        issues.append(Issue("future_date", WARNING, "The invoice date is in the future.", "invoice_date"))
    if invoice.invoice_date and invoice.due_date and invoice.due_date < invoice.invoice_date:
        issues.append(Issue("due_before_invoice_date", WARNING, "The due date is before the invoice date.", "due_date"))
    if invoice.currency and invoice.currency.upper() != "EUR":
        issues.append(
            Issue("foreign_currency", WARNING, f"Invoice is in {invoice.currency}; check the exchange rate.", "currency")
        )

    # 7. Does it match a purchase order?
    if invoice.po_number:
        po = purchase_order
        if po is None:
            issues.append(
                Issue("po_not_found", WARNING, f"Purchase order {invoice.po_number} is not in the system.", "po_number")
            )
        else:
            if invoice.gross_total is not None and not _close(
                invoice.gross_total, po.amount, max(1.0, 0.01 * po.amount)
            ):
                issues.append(
                    Issue(
                        "po_amount_mismatch",
                        WARNING,
                        f"Invoice total {invoice.gross_total:.2f} differs from purchase order amount {po.amount:.2f}.",
                        "gross_total",
                    )
                )
            if invoice.supplier_name and normalise_name(po.supplier_name) != normalise_name(invoice.supplier_name):
                issues.append(
                    Issue(
                        "po_supplier_mismatch",
                        WARNING,
                        f"Purchase order {po.po_number} was placed with {po.supplier_name}, not {invoice.supplier_name}.",
                        "supplier_name",
                    )
                )

    return issues


def decide_status(issues: list[Issue], *, auto_approve_with_warnings: bool = False) -> str:
    """Auto-approve only when nothing is wrong. Errors always go to a human."""
    if any(i.severity == ERROR for i in issues):
        return STATUS_NEEDS_REVIEW
    if issues and not auto_approve_with_warnings:
        return STATUS_NEEDS_REVIEW
    return STATUS_APPROVED
