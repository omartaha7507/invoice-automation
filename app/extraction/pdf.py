"""Small PDF helpers built on pypdf."""
from __future__ import annotations

from io import BytesIO
from typing import Optional

from pypdf import PdfReader

# File names used by ZUGFeRD / Factur-X / XRechnung for the embedded XML.
EMBEDDED_XML_NAMES = ("factur-x.xml", "zugferd-invoice.xml", "xrechnung.xml")


def pdf_text(data: bytes) -> str:
    """Text layer of a digital PDF. Scans have no text layer and return (almost) nothing."""
    try:
        reader = PdfReader(BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:  # a broken PDF is handled by falling back to the vision path
        return ""


def find_embedded_einvoice(data: bytes) -> Optional[bytes]:
    """Return the XML invoice attached inside a ZUGFeRD / Factur-X PDF, if there is one."""
    try:
        attachments = PdfReader(BytesIO(data)).attachments
    except Exception:
        return None
    by_name = {name.lower(): contents for name, contents in attachments.items() if contents}
    for name in EMBEDDED_XML_NAMES:
        if name in by_name:
            return by_name[name][0]
    for name, contents in by_name.items():
        if name.endswith(".xml"):
            return contents[0]
    return None
