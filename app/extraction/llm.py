"""Document classification and data extraction with Gemini (free tier).

Reliability measures, because a production job runs unattended:
  * structured output: the model must answer in the LLMInvoice JSON schema
  * validation: the answer is re-checked with Pydantic, never trusted blindly
  * rate limiting: stays under the free-tier requests-per-minute limit
  * retries with exponential backoff on rate limits, server errors and malformed answers
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import date, datetime

from pydantic import ValidationError

from app.extraction.base import ExtractionError
from app.schemas import ExtractedInvoice, LineItemData, LLMInvoice

log = logging.getLogger(__name__)

PROMPT = """You are the data-extraction step of an accounts-payable system at a German company.
Read the business document and return its data in the required JSON schema.

Rules:
- Only return what is printed on the document. If a value is not there, return null.
  Never calculate, guess or "fix" values - mistakes on the document must be preserved,
  because later checks are designed to catch them.
- document_type: "invoice", "credit_note" (Gutschrift / credit note), "delivery_note"
  (Lieferschein, usually without prices) or "other".
- supplier = the company that ISSUED the document; buyer = the company it is addressed to.
- supplier_vat_id: the VAT ID (USt-IdNr., VAT reg. no.), e.g. DE123456789. Not the Steuernummer.
- Dates as YYYY-MM-DD. German dates are DD.MM.YYYY.
- Amounts as plain numbers with a dot as decimal separator. German "1.234,56" means 1234.56.
- net_total = total before VAT, vat_total = VAT amount, gross_total = total payable including VAT.
- vat_rate as a percentage number (19, not 0.19).
- po_number: the buyer's purchase order reference (Bestellnummer, Ihre Bestellung, PO), if any.
- currency as an ISO code (EUR for €).
"""

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class RateLimiter:
    """Ensures a minimum gap between requests. Thread-safe."""

    def __init__(self, requests_per_minute: float) -> None:
        self.interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed:
                time.sleep(self._next_allowed - now)
                now = time.monotonic()
            self._next_allowed = now + self.interval


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def parse_llm_json(raw: str) -> ExtractedInvoice:
    """Turn the model's JSON answer into an ExtractedInvoice. Raises ValueError if invalid."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        llm = LLMInvoice.model_validate(json.loads(cleaned))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"LLM returned invalid data: {exc}") from exc

    return ExtractedInvoice(
        document_type=llm.document_type,
        supplier_name=_clean(llm.supplier_name),
        supplier_vat_id=_clean(llm.supplier_vat_id),
        buyer_name=_clean(llm.buyer_name),
        invoice_number=_clean(llm.invoice_number),
        invoice_date=_parse_date(llm.invoice_date),
        due_date=_parse_date(llm.due_date),
        po_number=_clean(llm.po_number),
        currency=(_clean(llm.currency) or "EUR").upper(),
        net_total=llm.net_total,
        vat_total=llm.vat_total,
        gross_total=llm.gross_total,
        line_items=[
            LineItemData(
                description=li.description.strip(),
                quantity=li.quantity,
                unit_price=li.unit_price,
                line_total=li.line_total,
                vat_rate=li.vat_rate,
            )
            for li in llm.line_items
        ],
    )


class GeminiExtractor:
    def __init__(self, api_key: str, model: str, requests_per_minute: float = 4, max_retries: int = 4) -> None:
        from google import genai  # imported here so the rest of the app works without the SDK
        from google.genai import types

        self._types = types
        self._client = genai.Client(api_key=api_key)
        self.model = model
        self.max_retries = max_retries
        self._limiter = RateLimiter(requests_per_minute)

    def extract_from_text(self, text: str) -> ExtractedInvoice:
        return self._call([PROMPT, "Document text:\n\n" + text])

    def extract_from_file(self, data: bytes, mime_type: str) -> ExtractedInvoice:
        return self._call([self._types.Part.from_bytes(data=data, mime_type=mime_type), PROMPT])

    def _call(self, contents: list) -> ExtractedInvoice:
        config = self._types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=LLMInvoice,
            temperature=0,
        )
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._limiter.wait()
            try:
                response = self._client.models.generate_content(model=self.model, contents=contents, config=config)
                return parse_llm_json(response.text or "")
            except ValueError as exc:  # malformed answer: asking again usually fixes it
                last_error = exc
            except Exception as exc:
                status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                if status not in RETRYABLE_STATUS:
                    raise ExtractionError(f"Gemini request failed: {exc}") from exc
                # Retrying can't succeed until the daily quota resets and only wastes requests.
                if status == 429 and "PerDay" in str(exc):
                    raise ExtractionError(f"Daily Gemini quota used up; try again after it resets. ({exc})") from exc
                last_error = exc
            if attempt < self.max_retries:
                delay = min(60, 2 ** (attempt + 1))
                log.warning("Extraction attempt %d failed (%s); retrying in %ss", attempt + 1, last_error, delay)
                time.sleep(delay)
        raise ExtractionError(f"Gave up after {self.max_retries + 1} attempts: {last_error}")
