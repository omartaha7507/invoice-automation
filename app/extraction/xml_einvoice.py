"""Parser for German/EU structured e-invoices.

Since 2025 every German business must be able to receive e-invoices. Two syntaxes exist:
  * UN/CEFACT CII  - used by ZUGFeRD / Factur-X and allowed for XRechnung
  * OASIS UBL 2.1  - the other XRechnung syntax
Structured data needs no AI at all: it is parsed exactly, which is faster, free and 100% accurate.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from defusedxml.ElementTree import fromstring  # safe against XML bombs and external entities

from app.schemas import DocumentType, ExtractedInvoice, LineItemData

CII = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}
UBL_INVOICE_NS = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
UBL_CREDIT_NS = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
UBL = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
}


class EInvoiceParseError(ValueError):
    pass


def _text(element, path: str, ns: dict[str, str]) -> Optional[str]:
    node = element.find(path, ns)
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


def _amount(value: Optional[str]) -> Optional[float]:
    return round(float(value), 2) if value is not None else None


def _number(value: Optional[str]) -> Optional[float]:
    return float(value) if value is not None else None


def _cii_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):  # format code 102 is YYYYMMDD
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _iso_date(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


def parse_einvoice_xml(data: bytes) -> ExtractedInvoice:
    try:
        root = fromstring(data)
    except Exception as exc:
        raise EInvoiceParseError(f"Not well-formed XML: {exc}") from exc

    tag = root.tag
    if tag == f"{{{CII['rsm']}}}CrossIndustryInvoice":
        return _parse_cii(root)
    if tag in (f"{{{UBL_INVOICE_NS}}}Invoice", f"{{{UBL_CREDIT_NS}}}CreditNote"):
        return _parse_ubl(root, is_credit_note=tag.endswith("CreditNote"))
    raise EInvoiceParseError("XML is not a CII (ZUGFeRD/Factur-X) or UBL (XRechnung) invoice.")


def _parse_cii(root) -> ExtractedInvoice:
    ns = CII
    tx = "rsm:SupplyChainTradeTransaction"
    agreement = f"{tx}/ram:ApplicableHeaderTradeAgreement"
    seller = f"{agreement}/ram:SellerTradeParty"
    settlement = f"{tx}/ram:ApplicableHeaderTradeSettlement"
    sums = f"{settlement}/ram:SpecifiedTradeSettlementHeaderMonetarySummation"

    vat_id = None
    for reg in root.findall(f"{seller}/ram:SpecifiedTaxRegistration/ram:ID", ns):
        if reg.get("schemeID") == "VA" and reg.text:
            vat_id = reg.text.strip()

    lines = []
    for item in root.findall(f"{tx}/ram:IncludedSupplyChainTradeLineItem", ns):
        lines.append(
            LineItemData(
                description=_text(item, "ram:SpecifiedTradeProduct/ram:Name", ns) or "",
                quantity=_number(_text(item, "ram:SpecifiedLineTradeDelivery/ram:BilledQuantity", ns)),
                unit_price=_amount(
                    _text(item, "ram:SpecifiedLineTradeAgreement/ram:NetPriceProductTradePrice/ram:ChargeAmount", ns)
                ),
                line_total=_amount(
                    _text(
                        item,
                        "ram:SpecifiedLineTradeSettlement/ram:SpecifiedTradeSettlementLineMonetarySummation/ram:LineTotalAmount",
                        ns,
                    )
                ),
                vat_rate=_number(
                    _text(item, "ram:SpecifiedLineTradeSettlement/ram:ApplicableTradeTax/ram:RateApplicablePercent", ns)
                ),
            )
        )

    type_code = _text(root, "rsm:ExchangedDocument/ram:TypeCode", ns)
    return ExtractedInvoice(
        document_type=DocumentType.CREDIT_NOTE if type_code == "381" else DocumentType.INVOICE,
        supplier_name=_text(root, f"{seller}/ram:Name", ns),
        supplier_vat_id=vat_id,
        buyer_name=_text(root, f"{agreement}/ram:BuyerTradeParty/ram:Name", ns),
        invoice_number=_text(root, "rsm:ExchangedDocument/ram:ID", ns),
        invoice_date=_cii_date(_text(root, "rsm:ExchangedDocument/ram:IssueDateTime/udt:DateTimeString", ns)),
        due_date=_cii_date(
            _text(root, f"{settlement}/ram:SpecifiedTradePaymentTerms/ram:DueDateDateTime/udt:DateTimeString", ns)
        ),
        po_number=_text(root, f"{agreement}/ram:BuyerOrderReferencedDocument/ram:IssuerAssignedID", ns),
        currency=_text(root, f"{settlement}/ram:InvoiceCurrencyCode", ns) or "EUR",
        net_total=_amount(_text(root, f"{sums}/ram:TaxBasisTotalAmount", ns)),
        vat_total=_amount(_text(root, f"{sums}/ram:TaxTotalAmount", ns)),
        gross_total=_amount(_text(root, f"{sums}/ram:GrandTotalAmount", ns)),
        line_items=lines,
    )


def _parse_ubl(root, *, is_credit_note: bool) -> ExtractedInvoice:
    ns = UBL
    supplier = "cac:AccountingSupplierParty/cac:Party"
    line_tag, qty_tag = ("cac:CreditNoteLine", "cbc:CreditedQuantity") if is_credit_note else (
        "cac:InvoiceLine",
        "cbc:InvoicedQuantity",
    )

    lines = []
    for item in root.findall(line_tag, ns):
        lines.append(
            LineItemData(
                description=_text(item, "cac:Item/cbc:Name", ns) or "",
                quantity=_number(_text(item, qty_tag, ns)),
                unit_price=_amount(_text(item, "cac:Price/cbc:PriceAmount", ns)),
                line_total=_amount(_text(item, "cbc:LineExtensionAmount", ns)),
                vat_rate=_number(_text(item, "cac:Item/cac:ClassifiedTaxCategory/cbc:Percent", ns)),
            )
        )

    supplier_name = _text(root, f"{supplier}/cac:PartyLegalEntity/cbc:RegistrationName", ns) or _text(
        root, f"{supplier}/cac:PartyName/cbc:Name", ns
    )
    buyer_name = _text(root, "cac:AccountingCustomerParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName", ns) or _text(
        root, "cac:AccountingCustomerParty/cac:Party/cac:PartyName/cbc:Name", ns
    )
    due = _text(root, "cbc:DueDate", ns) or _text(root, "cac:PaymentMeans/cbc:PaymentDueDate", ns)

    return ExtractedInvoice(
        document_type=DocumentType.CREDIT_NOTE if is_credit_note else DocumentType.INVOICE,
        supplier_name=supplier_name,
        supplier_vat_id=_text(root, f"{supplier}/cac:PartyTaxScheme/cbc:CompanyID", ns),
        buyer_name=buyer_name,
        invoice_number=_text(root, "cbc:ID", ns),
        invoice_date=_iso_date(_text(root, "cbc:IssueDate", ns)),
        due_date=_iso_date(due),
        po_number=_text(root, "cac:OrderReference/cbc:ID", ns),
        currency=_text(root, "cbc:DocumentCurrencyCode", ns) or "EUR",
        net_total=_amount(_text(root, "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", ns)),
        vat_total=_amount(_text(root, "cac:TaxTotal/cbc:TaxAmount", ns)),
        gross_total=_amount(_text(root, "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", ns)),
        line_items=lines,
    )
