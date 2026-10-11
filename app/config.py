"""Application settings, read once from environment variables (and a local .env file)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.data_dir = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
        self.database_url = os.getenv("DATABASE_URL", f"sqlite:///{self.data_dir / 'invoices.db'}")

        # LLM extraction
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "")
        self.gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
        self.llm_requests_per_minute = float(os.getenv("LLM_REQUESTS_PER_MINUTE", "4"))
        self.llm_max_retries = int(os.getenv("LLM_MAX_RETRIES", "4"))
        self.redact_pii = _bool("REDACT_PII", True)

        # Routing policy
        self.auto_approve_with_warnings = _bool("AUTO_APPROVE_WITH_WARNINGS", False)

        # Downstream accounting system
        self.erp_mode = os.getenv("ERP_MODE", "mock")  # "mock" or "webhook"
        self.erp_webhook_url = os.getenv("ERP_WEBHOOK_URL", "")

        # Reference data and limits
        self.purchase_orders_csv = os.getenv("PURCHASE_ORDERS_CSV", "")
        self.max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", "15"))

        # Folder and email ingestion
        self.watch_interval_seconds = int(os.getenv("WATCH_INTERVAL_SECONDS", "30"))
        self.imap_host = os.getenv("IMAP_HOST", "")
        self.imap_user = os.getenv("IMAP_USER", "")
        self.imap_password = os.getenv("IMAP_PASSWORD", "")
        self.imap_folder = os.getenv("IMAP_FOLDER", "INBOX")

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def failed_dir(self) -> Path:
        return self.data_dir / "failed"

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def erp_outbox_dir(self) -> Path:
        return self.data_dir / "erp_outbox"

    def ensure_dirs(self) -> None:
        for directory in (
            self.data_dir,
            self.inbox_dir,
            self.processed_dir,
            self.failed_dir,
            self.documents_dir,
            self.erp_outbox_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


settings = Settings()
