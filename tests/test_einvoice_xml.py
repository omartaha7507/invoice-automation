import pytest

from app.extraction.pdf import find_embedded_einvoice
from app.extraction.xml_einvoice import EInvoiceParseError, parse_einvoice_xml
from evaluation.synth import build_cii_xml, build_ubl_xml, build_zugferd_pdf, render_pdf


def assert_matches(parsed, facts):
    expected = facts.expected()
    data = parsed.model_dump(mode="json")
    for key in ("supplier_name", "supplier_vat_id", "buyer_name", "invoice_number", "invoice_date",
                "due_date", "po_number", "currency", "net_total", "vat_total", "gross_total"):
        assert data[key] == expected[key], key
    assert [li["line_total"] for li in data["line_items"]] == [li["line_total"] for li in expected["line_items"]]


def test_parse_cii_zugferd_xml(facts):
    assert_matches(parse_einvoice_xml(build_cii_xml(facts)), facts)


def test_parse_ubl_xrechnung_xml(facts):
    assert_matches(parse_einvoice_xml(build_ubl_xml(facts)), facts)


def test_xml_embedded_in_zugferd_pdf(facts):
    xml = find_embedded_einvoice(build_zugferd_pdf(facts, "de", "classic"))
    assert xml is not None
    assert_matches(parse_einvoice_xml(xml), facts)


def test_plain_pdf_has_no_embedded_xml(facts):
    assert find_embedded_einvoice(render_pdf(facts, "de", "classic")) is None


def test_other_xml_is_rejected():
    with pytest.raises(EInvoiceParseError):
        parse_einvoice_xml(b"<note><to>Omar</to></note>")


def test_xml_bomb_is_refused():
    bomb = b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]><lolz>&lol2;</lolz>"""
    with pytest.raises(EInvoiceParseError):
        parse_einvoice_xml(bomb)
