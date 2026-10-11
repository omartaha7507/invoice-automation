import pytest
from fastapi.testclient import TestClient

from app.api import app, get_pipeline
from app.database import get_db
from evaluation.synth import build_cii_xml, render_pdf


@pytest.fixture
def client(session_factory, pipeline):
    def override_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_pipeline] = lambda: pipeline
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def upload(client, name, data, mime):
    response = client.post("/api/documents", files=[("files", (name, data, mime))])
    assert response.status_code == 200
    return response.json()[0]


def test_upload_xml_list_detail_and_export(client, facts):
    result = upload(client, "e-invoice.xml", build_cii_xml(facts), "application/xml")
    assert result["status"] in ("approved", "needs_review")
    invoice_id = result["invoice_id"]

    listed = client.get("/api/invoices").json()
    assert [i["id"] for i in listed] == [invoice_id]

    detail = client.get(f"/api/invoices/{invoice_id}").json()
    assert detail["invoice_number"] == facts.number
    assert len(detail["line_items"]) == len(facts.lines)
    assert detail["audit_entries"][0]["action"] == "processed"

    original = client.get(f"/api/invoices/{invoice_id}/file")
    assert original.status_code == 200 and original.content.startswith(b"<?xml")

    stats = client.get("/api/stats").json()
    assert stats["total"] == 1

    csv_response = client.get(f"/api/export.csv?status={result['status']}")
    assert csv_response.text.startswith("\ufeffid;supplier;")
    assert facts.number in csv_response.text


def test_review_then_approve_with_correction(client, fake_extractor, clean_invoice, facts):
    fake_extractor.result = clean_invoice(gross_total=129.0)
    result = upload(client, "invoice.pdf", render_pdf(facts, "de", "classic"), "application/pdf")
    assert result["status"] == "needs_review"

    review_queue = client.get("/api/invoices?status=needs_review").json()
    assert review_queue[0]["issues"][0]["code"] == "totals_mismatch"

    corrections = clean_invoice().model_dump(mode="json")
    response = client.post(f"/api/invoices/{result['invoice_id']}/approve", json={"corrections": corrections, "reviewer": "anna"})
    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    again = client.post(f"/api/invoices/{result['invoice_id']}/approve", json={})
    assert again.status_code == 409


def test_reject_needs_a_reason(client, fake_extractor, clean_invoice, facts):
    fake_extractor.result = clean_invoice(gross_total=129.0)
    result = upload(client, "invoice.pdf", render_pdf(facts, "de", "classic"), "application/pdf")
    assert client.post(f"/api/invoices/{result['invoice_id']}/reject", json={"reason": ""}).status_code == 422
    ok = client.post(f"/api/invoices/{result['invoice_id']}/reject", json={"reason": "not ours"})
    assert ok.json()["status"] == "rejected"


def test_bad_uploads_are_explained(client):
    result = upload(client, "notes.txt", b"hello", "text/plain")
    assert result["invoice_id"] is None and "not supported" in result["error"]
    assert client.get("/api/invoices/999").status_code == 404
    assert client.get("/api/invoices?status=banana").status_code == 422


def test_health_and_ui(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert "Invoice desk" in client.get("/").text
