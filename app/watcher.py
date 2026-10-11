"""Background worker: picks up files from the inbox folder (and optionally an email inbox).

Run:  python -m app.watcher
Anything dropped into data/inbox/ is processed and moved to data/processed/ or data/failed/.
"""
from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal, init_db
from app.email_ingest import fetch_new_attachments
from app.factory import build_pipeline
from app.pipeline import InvoicePipeline, UnsupportedFileType, load_purchase_orders

log = logging.getLogger("invoice_watcher")

SETTLE_SECONDS = 2  # skip files that are still being copied in


def process_inbox(
    pipeline: InvoicePipeline,
    session_factory: Callable[[], Session],
    inbox: Path,
    processed_dir: Path,
    failed_dir: Path,
) -> int:
    handled = 0
    for path in sorted(p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")):
        if time.time() - path.stat().st_mtime < SETTLE_SECONDS:
            continue
        source = "email" if path.name.startswith("email-") else "folder"
        target_dir = processed_dir
        with session_factory() as db:
            try:
                invoice, is_new = pipeline.process(db, path.read_bytes(), path.name, source=source)
                log.info("%s -> invoice #%s (%s)%s", path.name, invoice.id, invoice.status, "" if is_new else " [repeat]")
            except UnsupportedFileType as exc:
                log.warning("Skipped %s: %s", path.name, exc)
                target_dir = failed_dir
            except Exception:
                log.exception("Could not process %s", path.name)
                target_dir = failed_dir
        shutil.move(str(path), target_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{path.name}")
        handled += 1
    return handled


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.ensure_dirs()
    init_db()
    if settings.purchase_orders_csv and Path(settings.purchase_orders_csv).exists():
        with SessionLocal() as db:
            load_purchase_orders(db, Path(settings.purchase_orders_csv))
    pipeline = build_pipeline(settings)
    log.info("Watching %s every %ss", settings.inbox_dir, settings.watch_interval_seconds)

    while True:
        if settings.imap_host:
            try:
                fetch_new_attachments(
                    settings.imap_host, settings.imap_user, settings.imap_password,
                    settings.imap_folder, settings.inbox_dir,
                )
            except Exception:
                log.exception("Email check failed; will retry next round")
        try:
            process_inbox(pipeline, SessionLocal, settings.inbox_dir, settings.processed_dir, settings.failed_dir)
        except Exception:
            log.exception("Inbox round failed; will retry next round")
        time.sleep(settings.watch_interval_seconds)


if __name__ == "__main__":
    main()
