"""The extractor interface. The pipeline only knows this, so the LLM provider is swappable
(and tests use a fake one that never calls the network)."""
from __future__ import annotations

from typing import Protocol

from app.schemas import ExtractedInvoice


class ExtractionError(RuntimeError):
    """The document could not be turned into structured data."""


class Extractor(Protocol):
    def extract_from_text(self, text: str) -> ExtractedInvoice: ...

    def extract_from_file(self, data: bytes, mime_type: str) -> ExtractedInvoice: ...


class UnconfiguredExtractor:
    """Used when no API key is set: XML e-invoices still work, everything else goes to review."""

    MESSAGE = "GEMINI_API_KEY is not set, so only XML e-invoices can be read automatically."

    def extract_from_text(self, text: str) -> ExtractedInvoice:
        raise ExtractionError(self.MESSAGE)

    def extract_from_file(self, data: bytes, mime_type: str) -> ExtractedInvoice:
        raise ExtractionError(self.MESSAGE)
