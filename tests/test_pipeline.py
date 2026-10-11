import json

import pytest

from app.constants import (
    METHOD_FAILED,
    METHOD_LLM_TEXT,
    METHOD_XML,
    METHOD_XML_EMBEDDED,
    STATUS_APPROVED,
    STATUS_NEEDS_REVIEW,
    STATUS_REJECTED,
)
from app.erp import ERPError
from app.extraction.base import ExtractionError
from app.models import Invoice
from app.pipeline import InvalidStateError, UnsupportedFileType, invoice_to_extracted
from evaluation.synth import build_cii_xml, build_zugferd_pdf, render_pdf


def actions(invoice):
    return [entry.action for entry in invoice.audit_entries]


def test_clean_pdf_is_approved_and_exported(db, pipeline, fake_extractor, clean_invoice, facts, tmp_path):
    fake_extractor.result = clean_invoice()
    invoice, is_new = pipeline.process(db, render_pdf(facts, "de", "classic"), "invoice.pdf")

    assert is_new
    assert invoice.status == STATUS_APPROVED
    assert invoice.extraction_method == METHOD_LLM_TEXT
    assert invoice.erp_reference.startswith("ERP-")
    assert len(invoice.line_items) == 2
    assert actions(invoice) == ["processed", "exported"]
    outbox = list((tmp_path / "outbox").glob("*.jsonl"))
    record = json.loads(outbox[0].read_text().splitlines()[0])
    assert record["invoice_number"] == "RE-2026-00042"


def test_text_sent_to_llm_is_redacted(db, tmp_path, clean_invoice, facts):
    seen = {}

    class Spy:
        def extract_from_text(self, text):
            seen["text"] = text
            return clean_invoice()

        def extract_from_file(self, data, mime_type):
            raise AssertionError("should use the text path")

    from app.erp import MockERPClient
    from app.pipeline import InvoicePipeline

    spy_pipeline = InvoicePipeline(Spy(), MockERPClient(tmp_path / "o"), tmp_path / "d")
    spy_pipeline.process(db, render_pdf(facts, "de", "classic"), "a.pdf")
    assert "[IBAN]" in seen["text"] and facts.supplier["iban"] not in seen["text"]
    assert facts.supplier["vat_id"] in seen["text"]


def test_same_file_twice_is_stored_once(db, pipeline, fake_extractor, clean_invoice, facts):
    fake_extractor.result = clean_invoice()
    data = render_pdf(facts, "de", "classic")
    first, _ = pipeline.process(db, data, "a.pdf")
    second, is_new = pipeline.process(db, data, "copy-of-a.pdf")
    assert not is_new and second.id == first.id
    assert db.query(Invoice).count() == 1
    assert len(fake_extractor.calls) == 1


def test_xml_einvoice_needs_no_llm(db, pipeline, fake_extractor, facts):
    invoice, _ = pipeline.process(db, build_cii_xml(facts), "x.xml")
    assert fake_extractor.calls == []
    assert invoice.extraction_method == METHOD_XML
    assert invoice.invoice_number == facts.number
    assert invoice.gross_total == facts.gross


def test_zugferd_pdf_uses_embedded_xml(db, pipeline, fake_extractor, facts):
    invoice, _ = pipeline.process(db, build_zugferd_pdf(facts, "en", "banner"), "z.pdf")
    assert fake_extractor.calls == []
    assert invoice.extraction_method == METHOD_XML_EMBEDDED


def test_extraction_failure_keeps_the_document(db, pipeline, fake_extractor, facts):
    fake_extractor.error = ExtractionError("model unavailable")
    invoice, _ = pipeline.process(db, render_pdf(facts, "de", "classic"), "a.pdf")
    assert invoice.status == STATUS_NEEDS_REVIEW
    assert invoice.extraction_method == METHOD_FAILED
    assert invoice.issues[0]["code"] == "extraction_failed"


def test_second_invoice_with_same_number_is_flagged(db, pipeline, fake_extractor, clean_invoice, facts):
    fake_extractor.result = clean_invoice()
    pipeline.process(db, render_pdf(facts, "de", "classic"), "original.pdf")
    duplicate, _ = pipeline.process(db, render_pdf(facts, "de", "classic", copy_mark=True), "copy.pdf")
    assert duplicate.status == STATUS_NEEDS_REVIEW
    assert [i["code"] for i in duplicate.issues] == ["duplicate_invoice"]


def test_reviewer_corrects_and_approves(db, pipeline, fake_extractor, clean_invoice, facts):
    fake_extractor.result = clean_invoice(gross_total=129.0)  # extractor misread the total
    invoice, _ = pipeline.process(db, render_pdf(facts, "de", "classic"), "a.pdf")
    assert invoice.status == STATUS_NEEDS_REVIEW

    approved = pipeline.approve(db, invoice, corrections=clean_invoice(), reviewer="anna")
    assert approved.status == STATUS_APPROVED
    assert approved.gross_total == 119.0
    assert approved.issues == []
    assert approved.erp_reference
    corrected = next(e for e in approved.audit_entries if e.action == "corrected")
    assert corrected.actor == "anna"
    assert corrected.details["changes"]["gross_total"] == {"from": 129.0, "to": 119.0}


def test_cannot_approve_twice_or_reject_after_export(db, pipeline, fake_extractor, clean_invoice, facts):
    fake_extractor.result = clean_invoice()
    invoice, _ = pipeline.process(db, render_pdf(facts, "de", "classic"), "a.pdf")
    with pytest.raises(InvalidStateError):
        pipeline.approve(db, invoice)
    with pytest.raises(InvalidStateError):
        pipeline.reject(db, invoice, reason="changed my mind")


def test_reject(db, pipeline, fake_extractor, clean_invoice, facts):
    fake_extractor.result = clean_invoice(gross_total=500.0)
    invoice, _ = pipeline.process(db, render_pdf(facts, "de", "classic"), "a.pdf")
    rejected = pipeline.reject(db, invoice, reason="not our order")
    assert rejected.status == STATUS_REJECTED
    assert actions(rejected)[-1] == "rejected"


def test_erp_failure_sends_invoice_back_to_review(db, tmp_path, fake_extractor, clean_invoice, facts):
    from app.pipeline import InvoicePipeline

    class DownERP:
        def push(self, payload):
            raise ERPError("connection refused")

    fake_extractor.result = clean_invoice()
    pipeline = InvoicePipeline(fake_extractor, DownERP(), tmp_path / "docs")
    invoice, _ = pipeline.process(db, render_pdf(facts, "de", "classic"), "a.pdf")
    assert invoice.status == STATUS_NEEDS_REVIEW
    assert invoice.erp_reference is None
    assert invoice.issues[-1]["code"] == "erp_export_failed"


def test_unsupported_file_type(db, pipeline):
    with pytest.raises(UnsupportedFileType):
        pipeline.process(db, b"hello", "notes.txt")


def test_round_trip_between_model_and_row(db, pipeline, fake_extractor, clean_invoice, facts):
    fake_extractor.result = clean_invoice(po_number=None)
    invoice, _ = pipeline.process(db, render_pdf(facts, "de", "classic"), "a.pdf")
    assert invoice_to_extracted(invoice) == clean_invoice(po_number=None)
