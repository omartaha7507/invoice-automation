from datetime import date, timedelta

import pytest

from app.constants import STATUS_APPROVED, STATUS_NEEDS_REVIEW
from app.schemas import DocumentType, ExtractedInvoice
from app.validation import PurchaseOrderRef, decide_status, validate_invoice, vat_id_problem


def codes(issues):
    return sorted(i.code for i in issues)


@pytest.mark.parametrize("vat_id", ["DE129274202", "DE143454214", "DE 811 569 869"])
def test_real_german_vat_ids_pass(vat_id):
    assert vat_id_problem(vat_id) is None


def test_wrong_check_digit_is_caught():
    assert vat_id_problem("DE129274203") == "checksum"


@pytest.mark.parametrize("vat_id", ["DE12345", "ATU1234", "12345678", "NL123456789X01"])
def test_bad_formats_are_caught(vat_id):
    assert vat_id_problem(vat_id) == "format"


def test_clean_invoice_has_no_issues(clean_invoice):
    issues = validate_invoice(clean_invoice())
    assert issues == []
    assert decide_status(issues) == STATUS_APPROVED


def test_line_items_not_adding_up(clean_invoice):
    issues = validate_invoice(clean_invoice(net_total=110.0, gross_total=129.0))
    assert codes(issues) == ["line_items_sum_mismatch"]
    assert decide_status(issues) == STATUS_NEEDS_REVIEW


def test_totals_not_adding_up(clean_invoice):
    assert codes(validate_invoice(clean_invoice(gross_total=120.0))) == ["totals_mismatch"]


def test_small_rounding_differences_are_tolerated(clean_invoice):
    assert validate_invoice(clean_invoice(gross_total=119.01)) == []


def test_duplicate_is_an_error(clean_invoice):
    issues = validate_invoice(clean_invoice(), is_duplicate=True)
    assert codes(issues) == ["duplicate_invoice"]
    assert decide_status(issues) == STATUS_NEEDS_REVIEW


def test_missing_required_fields(clean_invoice):
    issues = validate_invoice(clean_invoice(invoice_number=None, gross_total=None))
    assert {(i.code, i.field) for i in issues} >= {("missing_field", "invoice_number"), ("missing_field", "gross_total")}


def test_delivery_note_is_not_an_invoice():
    issues = validate_invoice(ExtractedInvoice(document_type=DocumentType.DELIVERY_NOTE))
    assert codes(issues) == ["not_an_invoice"]


def test_unknown_purchase_order(clean_invoice):
    assert codes(validate_invoice(clean_invoice(po_number="PO-1"))) == ["po_not_found"]


def test_purchase_order_amount_and_supplier_mismatch(clean_invoice):
    po = PurchaseOrderRef("PO-1", "Somebody Else AG", 200.0)
    assert codes(validate_invoice(clean_invoice(po_number="PO-1"), purchase_order=po)) == [
        "po_amount_mismatch",
        "po_supplier_mismatch",
    ]


def test_matching_purchase_order(clean_invoice):
    po = PurchaseOrderRef("PO-1", "Müller Bürobedarf GmbH", 119.0)
    assert validate_invoice(clean_invoice(po_number="PO-1"), purchase_order=po) == []


def test_unusual_vat_rate_for_german_supplier(clean_invoice):
    invoice = clean_invoice()
    invoice.line_items[0].vat_rate = 16
    assert codes(validate_invoice(invoice)) == ["unusual_vat_rate"]


def test_dates_and_currency(clean_invoice):
    future = date.today() + timedelta(days=3)
    issues = validate_invoice(clean_invoice(invoice_date=future, due_date=date.today(), currency="USD"))
    assert codes(issues) == ["due_before_invoice_date", "foreign_currency", "future_date"]


def test_warnings_can_be_auto_approved_if_configured(clean_invoice):
    issues = validate_invoice(clean_invoice(po_number="PO-1"))
    assert decide_status(issues) == STATUS_NEEDS_REVIEW
    assert decide_status(issues, auto_approve_with_warnings=True) == STATUS_APPROVED


def test_errors_are_never_auto_approved(clean_invoice):
    issues = validate_invoice(clean_invoice(gross_total=999.0))
    assert decide_status(issues, auto_approve_with_warnings=True) == STATUS_NEEDS_REVIEW
