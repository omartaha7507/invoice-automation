"""Synthetic invoice generator for testing and evaluation.

Creates realistic German/English invoices in every format the pipeline accepts (digital PDF,
scanned image, XRechnung UBL XML, ZUGFeRD CII XML, ZUGFeRD PDF with embedded XML), with
deliberate errors mixed in. Because we generate them, we know the correct answer for each:
that is the "ground truth" the evaluation scores against.
"""
from __future__ import annotations

import io
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from xml.sax.saxutils import escape

from app.redaction import iban_is_valid
from app.validation import german_vat_check_digit

BUYER = {"name": "Spreewerk Handel GmbH", "street": "Friedrichstraße 200", "city": "10117 Berlin"}

SUPPLIERS = [
    ("Müller Bürobedarf GmbH", "Hauptstraße 12", "10115 Berlin", "DE"),
    ("Schneider Logistik GmbH & Co. KG", "Am Hafen 4", "20457 Hamburg", "DE"),
    ("Becker IT-Services GmbH", "Leopoldstraße 88", "80802 München", "DE"),
    ("Wagner Verpackungen AG", "Industriering 7", "50829 Köln", "DE"),
    ("Hoffmann Reinigungsdienst GmbH", "Karl-Marx-Allee 101", "10243 Berlin", "DE"),
    ("Fischer Druck & Medien GmbH", "Königstraße 21", "70173 Stuttgart", "DE"),
    ("Nordsee Kaffeerösterei GmbH", "Deichweg 3", "26382 Wilhelmshaven", "DE"),
    ("Klein Elektrotechnik GmbH", "Bahnhofstraße 45", "04109 Leipzig", "DE"),
    ("Alpen Software GmbH", "Mariahilfer Straße 50", "1070 Wien", "AT"),
    ("Van Dijk Transport B.V.", "Kade 12", "3011 Rotterdam", "NL"),
]

# (German description, English description, min price, max price, VAT rate)
PRODUCTS = [
    ("Kopierpapier A4, 500 Blatt", "Copy paper A4, 500 sheets", 4.5, 7.9, 19.0),
    ("Tonerkartusche schwarz", "Toner cartridge black", 49.0, 119.0, 19.0),
    ("Unterhaltsreinigung (Stunden)", "Office cleaning (hours)", 28.0, 39.0, 19.0),
    ("Palettentransport Berlin-Hamburg", "Pallet transport Berlin-Hamburg", 85.0, 240.0, 19.0),
    ("Kaffeebohnen 1 kg", "Coffee beans 1 kg", 14.0, 26.0, 7.0),
    ("Fachbuch Steuerrecht", "Tax law handbook", 29.0, 79.0, 7.0),
    ("Softwarelizenz (Monat)", "Software licence (month)", 19.0, 249.0, 19.0),
    ("Monitorhalterung", "Monitor arm", 39.0, 89.0, 19.0),
    ("Netzwerkkabel Cat6, 5 m", "Network cable Cat6, 5 m", 4.0, 12.0, 19.0),
    ("Versandkartons 60x40x40", "Shipping boxes 60x40x40", 1.2, 3.5, 19.0),
    ("Wartung Klimaanlage", "Air-conditioning maintenance", 120.0, 380.0, 19.0),
    ("Visitenkarten, 500 Stück", "Business cards, 500 pcs", 35.0, 65.0, 19.0),
]

LABELS = {
    "de": {
        "invoice": "Rechnung", "delivery": "Lieferschein", "number": "Rechnungsnummer",
        "delivery_number": "Lieferschein-Nr.", "date": "Rechnungsdatum", "due": "Zahlbar bis",
        "po": "Ihre Bestellnummer", "to": "Rechnungsempfänger", "desc": "Beschreibung", "qty": "Menge",
        "unit": "Einzelpreis", "rate": "USt.", "total": "Gesamt", "net": "Nettobetrag",
        "vat": "zzgl. USt. {rate}%", "gross": "Rechnungsbetrag", "vatid": "USt-IdNr.", "bank": "Bankverbindung",
        "tel": "Tel.", "mail": "E-Mail", "thanks": "Vielen Dank für Ihren Auftrag.", "copy": "KOPIE",
        "number_short": "Nr.", "of": "vom", "ref": "Bezug: Bestellung",
    },
    "en": {
        "invoice": "Invoice", "delivery": "Delivery note", "number": "Invoice number",
        "delivery_number": "Delivery note no.", "date": "Invoice date", "due": "Due date",
        "po": "Your PO number", "to": "Bill to", "desc": "Description", "qty": "Qty",
        "unit": "Unit price", "rate": "VAT", "total": "Amount", "net": "Subtotal",
        "vat": "VAT {rate}%", "gross": "Total due", "vatid": "VAT reg. no.", "bank": "Bank details",
        "tel": "Phone", "mail": "Email", "thanks": "Thank you for your business.", "copy": "COPY",
        "number_short": "No.", "of": "dated", "ref": "Reference: purchase order",
    },
}

SCENARIO_ISSUES = {
    "clean": [],
    "line_sum_mismatch": ["line_items_sum_mismatch"],
    "totals_mismatch": ["totals_mismatch"],
    "invalid_vat_id": ["invalid_vat_id"],
    "duplicate": ["duplicate_invoice"],
    "unknown_po": ["po_not_found"],
    "po_amount_mismatch": ["po_amount_mismatch"],
    "delivery_note": ["not_an_invoice"],
}


# --- Small helpers ------------------------------------------------------------------------


def make_de_vat_id(rng: random.Random, valid: bool = True) -> str:
    digits = str(rng.randint(1, 9)) + "".join(str(rng.randint(0, 9)) for _ in range(7))
    check = german_vat_check_digit(digits)
    if not valid:
        check = (check + rng.randint(1, 9)) % 10
    return f"DE{digits}{check}"


def make_iban(rng: random.Random, country: str = "DE") -> str:
    bban = "".join(str(rng.randint(0, 9)) for _ in range(18))
    numeric = "".join(str(int(ch, 36)) for ch in bban + country + "00")
    iban = f"{country}{98 - int(numeric) % 97:02d}{bban}"
    assert iban_is_valid(iban)
    return " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))


def fmt_money(value: float, lang: str) -> str:
    text = f"{value:,.2f}"
    if lang == "de":
        return text.replace(",", "X").replace(".", ",").replace("X", ".") + " €"
    return "€" + text


def fmt_qty(value: float, lang: str) -> str:
    text = f"{value:g}"
    return text.replace(".", ",") if lang == "de" else text


def fmt_date(value: date, lang: str, style: int) -> str:
    if lang == "de":
        return value.strftime("%d.%m.%Y")
    return value.strftime("%d %b %Y") if style == 0 else value.isoformat()


# --- The invoice "facts" ------------------------------------------------------------------


@dataclass
class Facts:
    supplier: dict[str, str]
    number: str
    issue_date: date
    due_date: date
    po_number: str | None
    lines: list[dict[str, Any]]
    net: float
    vat: float
    gross: float
    vat_breakdown: dict[float, float] = field(default_factory=dict)
    document_type: str = "invoice"
    currency: str = "EUR"

    def expected(self) -> dict[str, Any]:
        return {
            "document_type": self.document_type,
            "supplier_name": self.supplier["name"],
            "supplier_vat_id": self.supplier["vat_id"],
            "buyer_name": BUYER["name"],
            "invoice_number": self.number,
            "invoice_date": self.issue_date.isoformat(),
            "due_date": self.due_date.isoformat(),
            "po_number": self.po_number,
            "currency": self.currency,
            "net_total": self.net,
            "vat_total": self.vat,
            "gross_total": self.gross,
            "line_items": [
                {"description": ln["desc"], "quantity": ln["qty"], "unit_price": ln["unit"],
                 "line_total": ln["total"], "vat_rate": ln["rate"]}
                for ln in self.lines
            ],
        }


def make_supplier(rng: random.Random, index: int) -> dict[str, str]:
    name, street, city, country = SUPPLIERS[index]
    if country == "DE":
        vat_id = make_de_vat_id(rng)
    elif country == "AT":
        vat_id = "ATU" + "".join(str(rng.randint(0, 9)) for _ in range(8))
    else:
        vat_id = "NL" + "".join(str(rng.randint(0, 9)) for _ in range(9)) + "B01"
    slug = name.split()[0].lower().replace("ü", "ue").replace("ö", "oe")
    return {
        "name": name, "street": street, "city": city, "country": country, "vat_id": vat_id,
        "iban": make_iban(rng), "phone": f"+49 30 {rng.randint(1000000, 9999999)}",
        "email": f"rechnung@{slug}.example", "number_style": str(rng.randint(0, 3)),
    }


def make_facts(rng: random.Random, supplier: dict[str, str], lang: str, today: date, counter: int) -> Facts:
    year = today.year
    style = int(supplier["number_style"])
    number = [f"RE-{year}-{counter:05d}", f"{year}{counter:06d}", f"INV/{counter:05d}", f"R{counter:07d}"][style]
    issue = today - timedelta(days=rng.randint(2, 60))
    lines = []
    for _ in range(rng.randint(1, 5)):
        de, en, low, high, rate = rng.choice(PRODUCTS)
        qty = float(rng.choice([1, 1, 2, 3, 4, 5, 10, 12, 20, 2.5]))
        unit = round(rng.uniform(low, high), 2)
        lines.append({"desc": de if lang == "de" else en, "qty": qty, "unit": unit,
                      "total": round(qty * unit, 2), "rate": rate})
    net = round(sum(ln["total"] for ln in lines), 2)
    breakdown: dict[float, float] = {}
    for rate in sorted({ln["rate"] for ln in lines}):
        base = sum(ln["total"] for ln in lines if ln["rate"] == rate)
        breakdown[rate] = round(base * rate / 100, 2)
    vat = round(sum(breakdown.values()), 2)
    return Facts(
        supplier=supplier, number=number, issue_date=issue,
        due_date=issue + timedelta(days=rng.choice([14, 30])),
        po_number=f"PO-{rng.randint(100000, 999999)}" if rng.random() < 0.7 else None,
        lines=lines, net=net, vat=vat, gross=round(net + vat, 2), vat_breakdown=breakdown,
    )


# --- Rendering: PDF -----------------------------------------------------------------------


def render_pdf(facts: Facts, lang: str, template: str, copy_mark: bool = False) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    L = LABELS[lang]
    s = facts.supplier
    is_delivery = facts.document_type == "delivery_note"
    date_style = len(facts.number) % 2
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    left, right = 50, width - 50

    def text(x, y, value, size=9.5, bold=False, align="left"):
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        if align == "right":
            c.drawRightString(x, y, value)
        else:
            c.drawString(x, y, value)

    title = L["delivery"] if is_delivery else L["invoice"]
    number_label = L["delivery_number"] if is_delivery else L["number"]
    issue = fmt_date(facts.issue_date, lang, date_style)
    due = fmt_date(facts.due_date, lang, date_style)

    if template == "classic":
        text(left, height - 60, f"{s['name']} · {s['street']} · {s['city']}", 7.5)
        y = height - 100
        text(left, y, L["to"] + ":", 8)
        for i, line in enumerate((BUYER["name"], BUYER["street"], BUYER["city"])):
            text(left, y - 14 * (i + 1), line, 10)
        meta = [(number_label, facts.number), (L["date"], issue)]
        if not is_delivery:
            meta.append((L["due"], due))
        if facts.po_number:
            meta.append((L["po"], facts.po_number))
        for i, (label, value) in enumerate(meta):
            text(330, height - 100 - 14 * i, label + ":", 9)
            text(right, height - 100 - 14 * i, value, 9, align="right")
        text(left, height - 210, title, 18, bold=True)
        y = height - 245
    elif template == "banner":
        c.setFillGray(0.92)
        c.rect(0, height - 110, width, 110, stroke=0, fill=1)
        c.setFillGray(0)
        text(left, height - 55, s["name"], 17, bold=True)
        text(left, height - 75, f"{s['street']}, {s['city']}", 9)
        text(left, height - 90, f"{L['vatid']} {s['vat_id']}", 9)
        text(right, height - 55, title.upper(), 14, bold=True, align="right")
        y = height - 140
        text(left, y, f"{L['to']}: {BUYER['name']}, {BUYER['street']}, {BUYER['city']}", 9.5)
        y -= 22
        text(left, y, f"{number_label}: {facts.number}", 9.5)
        text(300, y, f"{L['date']}: {issue}", 9.5)
        y -= 14
        if not is_delivery:
            text(left, y, f"{L['due']}: {due}", 9.5)
        if facts.po_number:
            text(300, y, f"{L['po']}: {facts.po_number}", 9.5)
        y -= 35
    else:  # compact
        text(left, height - 50, s["name"], 12, bold=True)
        text(left, height - 64, f"{s['street']} | {s['city']} | {L['vatid']} {s['vat_id']}", 8)
        text(left, height - 100, BUYER["name"], 10)
        text(left, height - 113, f"{BUYER['street']}, {BUYER['city']}", 10)
        y = height - 150
        text(left, y, f"{title} {L['number_short']} {facts.number} {L['of']} {issue}", 12, bold=True)
        y -= 16
        if facts.po_number:
            text(left, y, f"{L['ref']} {facts.po_number}", 9.5)
            y -= 14
        if not is_delivery:
            text(left, y, f"{L['due']}: {due}", 9.5)
        y -= 30

    if copy_mark:
        text(right, height - 30, L["copy"], 14, bold=True, align="right")

    # Line item table
    cols = (left, 300, 360, 440, right) if not is_delivery else (left, 400)
    text(cols[0], y, L["desc"], 9, bold=True)
    text(cols[1] + (30 if not is_delivery else 0), y, L["qty"], 9, bold=True, align="right" if not is_delivery else "left")
    if not is_delivery:
        text(cols[2] + 50, y, L["unit"], 9, bold=True, align="right")
        text(cols[3] + 30, y, L["rate"], 9, bold=True, align="right")
        text(cols[4], y, L["total"], 9, bold=True, align="right")
    c.line(left, y - 5, right, y - 5)
    y -= 20
    for line in facts.lines:
        text(cols[0], y, line["desc"], 9.5)
        if is_delivery:
            text(cols[1], y, fmt_qty(line["qty"], lang), 9.5)
        else:
            text(cols[1] + 30, y, fmt_qty(line["qty"], lang), 9.5, align="right")
            text(cols[2] + 50, y, fmt_money(line["unit"], lang), 9.5, align="right")
            text(cols[3] + 30, y, f"{line['rate']:g}%", 9.5, align="right")
            text(cols[4], y, fmt_money(line["total"], lang), 9.5, align="right")
        y -= 16
    c.line(left, y + 6, right, y + 6)

    if not is_delivery:
        y -= 12
        rows = [(L["net"], facts.net)] + [
            (L["vat"].format(rate=f"{rate:g}"), amount) for rate, amount in facts.vat_breakdown.items()
        ]
        for label, amount in rows:
            text(380, y, label, 9.5)
            text(right, y, fmt_money(amount, lang), 9.5, align="right")
            y -= 15
        text(380, y - 3, L["gross"], 11, bold=True)
        text(right, y - 3, fmt_money(facts.gross, lang), 11, bold=True, align="right")
        text(left, y - 50, L["thanks"], 9.5)

    # Footer with contact and bank data (redacted before any LLM call)
    c.line(left, 85, right, 85)
    text(left, 70, f"{s['name']} | {s['street']} | {s['city']}", 7.5)
    text(left, 58, f"{L['tel']} {s['phone']} | {L['mail']}: {s['email']}", 7.5)
    footer = f"{L['bank']}: IBAN {s['iban']}"
    if template == "classic":
        footer += f" | {L['vatid']} {s['vat_id']}"
    text(left, 46, footer, 7.5)

    c.showPage()
    c.save()
    return buffer.getvalue()


def render_scan(pdf_bytes: bytes, rng: random.Random) -> bytes:
    """Turn a PDF into a slightly rotated, grainy greyscale JPEG, like a phone photo or a scan."""
    import pypdfium2 as pdfium
    from PIL import Image, ImageFilter

    page = pdfium.PdfDocument(pdf_bytes)[0]
    image = page.render(scale=1.5).to_pil().convert("L")
    image = image.rotate(rng.uniform(-1.2, 1.2), expand=True, fillcolor=255)
    noise = Image.effect_noise(image.size, 18).convert("L")
    image = Image.blend(image, noise, 0.08).filter(ImageFilter.GaussianBlur(0.4))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=72, optimize=True)
    return out.getvalue()


# --- Rendering: structured e-invoices -----------------------------------------------------


def build_cii_xml(facts: Facts) -> bytes:
    """ZUGFeRD / Factur-X (EN 16931 profile) style CII XML."""
    s = facts.supplier
    e = lambda value: escape(str(value))
    lines = "".join(
        f"""
    <ram:IncludedSupplyChainTradeLineItem>
      <ram:AssociatedDocumentLineDocument><ram:LineID>{i}</ram:LineID></ram:AssociatedDocumentLineDocument>
      <ram:SpecifiedTradeProduct><ram:Name>{e(ln['desc'])}</ram:Name></ram:SpecifiedTradeProduct>
      <ram:SpecifiedLineTradeAgreement>
        <ram:NetPriceProductTradePrice><ram:ChargeAmount>{ln['unit']:.2f}</ram:ChargeAmount></ram:NetPriceProductTradePrice>
      </ram:SpecifiedLineTradeAgreement>
      <ram:SpecifiedLineTradeDelivery><ram:BilledQuantity unitCode="C62">{ln['qty']:g}</ram:BilledQuantity></ram:SpecifiedLineTradeDelivery>
      <ram:SpecifiedLineTradeSettlement>
        <ram:ApplicableTradeTax><ram:TypeCode>VAT</ram:TypeCode><ram:CategoryCode>S</ram:CategoryCode><ram:RateApplicablePercent>{ln['rate']:g}</ram:RateApplicablePercent></ram:ApplicableTradeTax>
        <ram:SpecifiedTradeSettlementLineMonetarySummation><ram:LineTotalAmount>{ln['total']:.2f}</ram:LineTotalAmount></ram:SpecifiedTradeSettlementLineMonetarySummation>
      </ram:SpecifiedLineTradeSettlement>
    </ram:IncludedSupplyChainTradeLineItem>"""
        for i, ln in enumerate(facts.lines, start=1)
    )
    taxes = "".join(
        f"""
      <ram:ApplicableTradeTax><ram:CalculatedAmount>{amount:.2f}</ram:CalculatedAmount><ram:TypeCode>VAT</ram:TypeCode><ram:BasisAmount>{sum(ln['total'] for ln in facts.lines if ln['rate'] == rate):.2f}</ram:BasisAmount><ram:CategoryCode>S</ram:CategoryCode><ram:RateApplicablePercent>{rate:g}</ram:RateApplicablePercent></ram:ApplicableTradeTax>"""
        for rate, amount in facts.vat_breakdown.items()
    )
    po = (
        f"<ram:BuyerOrderReferencedDocument><ram:IssuerAssignedID>{e(facts.po_number)}</ram:IssuerAssignedID></ram:BuyerOrderReferencedDocument>"
        if facts.po_number else ""
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100" xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100" xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocumentContext>
    <ram:GuidelineSpecifiedDocumentContextParameter><ram:ID>urn:cen.eu:en16931:2017</ram:ID></ram:GuidelineSpecifiedDocumentContextParameter>
  </rsm:ExchangedDocumentContext>
  <rsm:ExchangedDocument>
    <ram:ID>{e(facts.number)}</ram:ID>
    <ram:TypeCode>380</ram:TypeCode>
    <ram:IssueDateTime><udt:DateTimeString format="102">{facts.issue_date:%Y%m%d}</udt:DateTimeString></ram:IssueDateTime>
  </rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>{lines}
    <ram:ApplicableHeaderTradeAgreement>
      <ram:SellerTradeParty>
        <ram:Name>{e(s['name'])}</ram:Name>
        <ram:PostalTradeAddress><ram:LineOne>{e(s['street'])}</ram:LineOne><ram:CityName>{e(s['city'])}</ram:CityName><ram:CountryID>{s['country']}</ram:CountryID></ram:PostalTradeAddress>
        <ram:SpecifiedTaxRegistration><ram:ID schemeID="VA">{e(s['vat_id'])}</ram:ID></ram:SpecifiedTaxRegistration>
      </ram:SellerTradeParty>
      <ram:BuyerTradeParty><ram:Name>{e(BUYER['name'])}</ram:Name></ram:BuyerTradeParty>
      {po}
    </ram:ApplicableHeaderTradeAgreement>
    <ram:ApplicableHeaderTradeDelivery/>
    <ram:ApplicableHeaderTradeSettlement>
      <ram:InvoiceCurrencyCode>{facts.currency}</ram:InvoiceCurrencyCode>{taxes}
      <ram:SpecifiedTradePaymentTerms><ram:DueDateDateTime><udt:DateTimeString format="102">{facts.due_date:%Y%m%d}</udt:DateTimeString></ram:DueDateDateTime></ram:SpecifiedTradePaymentTerms>
      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        <ram:LineTotalAmount>{facts.net:.2f}</ram:LineTotalAmount>
        <ram:TaxBasisTotalAmount>{facts.net:.2f}</ram:TaxBasisTotalAmount>
        <ram:TaxTotalAmount currencyID="{facts.currency}">{facts.vat:.2f}</ram:TaxTotalAmount>
        <ram:GrandTotalAmount>{facts.gross:.2f}</ram:GrandTotalAmount>
        <ram:DuePayableAmount>{facts.gross:.2f}</ram:DuePayableAmount>
      </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
    </ram:ApplicableHeaderTradeSettlement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>
"""
    return xml.encode("utf-8")


def build_ubl_xml(facts: Facts) -> bytes:
    """XRechnung style UBL 2.1 XML."""
    s = facts.supplier
    e = lambda value: escape(str(value))
    cur = facts.currency
    lines = "".join(
        f"""
  <cac:InvoiceLine>
    <cbc:ID>{i}</cbc:ID>
    <cbc:InvoicedQuantity unitCode="C62">{ln['qty']:g}</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="{cur}">{ln['total']:.2f}</cbc:LineExtensionAmount>
    <cac:Item>
      <cbc:Name>{e(ln['desc'])}</cbc:Name>
      <cac:ClassifiedTaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>{ln['rate']:g}</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:ClassifiedTaxCategory>
    </cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="{cur}">{ln['unit']:.2f}</cbc:PriceAmount></cac:Price>
  </cac:InvoiceLine>"""
        for i, ln in enumerate(facts.lines, start=1)
    )
    po = f"<cac:OrderReference><cbc:ID>{e(facts.po_number)}</cbc:ID></cac:OrderReference>" if facts.po_number else ""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2" xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0</cbc:CustomizationID>
  <cbc:ID>{e(facts.number)}</cbc:ID>
  <cbc:IssueDate>{facts.issue_date.isoformat()}</cbc:IssueDate>
  <cbc:DueDate>{facts.due_date.isoformat()}</cbc:DueDate>
  <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>{cur}</cbc:DocumentCurrencyCode>
  <cbc:BuyerReference>{e(facts.po_number or "n/a")}</cbc:BuyerReference>
  {po}
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyName><cbc:Name>{e(s['name'])}</cbc:Name></cac:PartyName>
    <cac:PostalAddress><cbc:StreetName>{e(s['street'])}</cbc:StreetName><cbc:CityName>{e(s['city'])}</cbc:CityName><cac:Country><cbc:IdentificationCode>{s['country']}</cbc:IdentificationCode></cac:Country></cac:PostalAddress>
    <cac:PartyTaxScheme><cbc:CompanyID>{e(s['vat_id'])}</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
    <cac:PartyLegalEntity><cbc:RegistrationName>{e(s['name'])}</cbc:RegistrationName></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>
    <cac:PartyLegalEntity><cbc:RegistrationName>{e(BUYER['name'])}</cbc:RegistrationName></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingCustomerParty>
  <cac:TaxTotal><cbc:TaxAmount currencyID="{cur}">{facts.vat:.2f}</cbc:TaxAmount></cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="{cur}">{facts.net:.2f}</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="{cur}">{facts.net:.2f}</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="{cur}">{facts.gross:.2f}</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="{cur}">{facts.gross:.2f}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>{lines}
</Invoice>
"""
    return xml.encode("utf-8")


def build_zugferd_pdf(facts: Facts, lang: str, template: str) -> bytes:
    """A PDF a human can read with the machine-readable XML attached inside (hybrid e-invoice).
    Note: real ZUGFeRD files are PDF/A-3; this synthetic version only mimics the structure."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(render_pdf(facts, lang, template))))
    writer.add_attachment("factur-x.xml", build_cii_xml(facts))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
