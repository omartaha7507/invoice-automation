"""Builds the pipeline from settings, in one place for the API, the watcher and the eval."""
from __future__ import annotations

from app.config import Settings
from app.erp import ERPClient, MockERPClient, WebhookERPClient
from app.extraction.base import Extractor, UnconfiguredExtractor
from app.pipeline import InvoicePipeline


def build_extractor(settings: Settings) -> Extractor:
    if not settings.gemini_api_key:
        return UnconfiguredExtractor()
    from app.extraction.llm import GeminiExtractor

    return GeminiExtractor(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        requests_per_minute=settings.llm_requests_per_minute,
        max_retries=settings.llm_max_retries,
    )


def build_erp_client(settings: Settings) -> ERPClient:
    if settings.erp_mode == "webhook":
        if not settings.erp_webhook_url:
            raise ValueError("ERP_MODE=webhook needs ERP_WEBHOOK_URL")
        return WebhookERPClient(settings.erp_webhook_url)
    return MockERPClient(settings.erp_outbox_dir)


def build_pipeline(settings: Settings) -> InvoicePipeline:
    settings.ensure_dirs()
    return InvoicePipeline(
        extractor=build_extractor(settings),
        erp=build_erp_client(settings),
        documents_dir=settings.documents_dir,
        redact_pii=settings.redact_pii,
        auto_approve_with_warnings=settings.auto_approve_with_warnings,
    )
