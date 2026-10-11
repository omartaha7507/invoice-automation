"""Collect invoice attachments from an email inbox (IMAP), the way most invoices arrive."""
from __future__ import annotations

import email
import email.policy
import imaplib
import logging
from email.message import EmailMessage
from pathlib import Path

from app.constants import SUPPORTED_TYPES

log = logging.getLogger(__name__)


def save_attachments(message: EmailMessage, dest_dir: Path, prefix: str) -> list[Path]:
    """Save supported attachments (PDF, XML, images) of one email into dest_dir."""
    saved: list[Path] = []
    for part in message.iter_attachments():
        filename = part.get_filename()
        if not filename:
            continue
        safe_name = Path(filename).name.replace(" ", "_")
        if Path(safe_name).suffix.lower() not in SUPPORTED_TYPES:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        target = dest_dir / f"{prefix}-{safe_name}"
        target.write_bytes(payload)
        saved.append(target)
    return saved


def fetch_new_attachments(host: str, user: str, password: str, folder: str, dest_dir: Path) -> list[Path]:
    """Download attachments of all unread emails, then mark those emails as read."""
    saved: list[Path] = []
    with imaplib.IMAP4_SSL(host) as imap:
        imap.login(user, password)
        imap.select(folder)
        _, data = imap.uid("search", None, "UNSEEN")
        for uid in data[0].split():
            _, message_data = imap.uid("fetch", uid, "(RFC822)")
            raw = message_data[0][1]
            message = email.message_from_bytes(raw, policy=email.policy.default)
            saved += save_attachments(message, dest_dir, prefix=f"email-{uid.decode()}")
            imap.uid("store", uid, "+FLAGS", "(\\Seen)")
    if saved:
        log.info("Saved %d attachment(s) from %s", len(saved), host)
    return saved
