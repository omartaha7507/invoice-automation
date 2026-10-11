"""Sending approved invoices to the downstream accounting system (ERP).

A real company would connect SAP, DATEV, Lexoffice, etc. here. Two implementations:
  * MockERPClient    - writes each booking to a daily JSON-lines "outbox" file (default)
  * WebhookERPClient - POSTs JSON to any HTTP endpoint (e.g. an n8n/Zapier webhook or an ERP API)
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol


class ERPError(RuntimeError):
    pass


class ERPClient(Protocol):
    def push(self, payload: dict[str, Any]) -> str:
        """Send one invoice; return the reference the accounting system assigned."""
        ...


class MockERPClient:
    def __init__(self, outbox_dir: Path) -> None:
        self.outbox_dir = Path(outbox_dir)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)

    def push(self, payload: dict[str, Any]) -> str:
        now = datetime.now(UTC)
        reference = f"ERP-{now:%Y%m%d}-{payload['invoice_id']:06d}"
        record = {"erp_reference": reference, "received_at": now.isoformat(), **payload}
        path = self.outbox_dir / f"{now:%Y-%m-%d}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return reference


class WebhookERPClient:
    def __init__(self, url: str, timeout: float = 10, retries: int = 3) -> None:
        self.url = url
        self.timeout = timeout
        self.retries = retries

    def push(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, default=str).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            # Lets the receiver ignore a repeat if we retry after a timeout: no double booking.
            "Idempotency-Key": f"invoice-{payload['invoice_id']}",
        }
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    content = response.read()
                try:
                    reference = json.loads(content or b"{}").get("reference")
                except (json.JSONDecodeError, AttributeError):
                    reference = None
                return str(reference or f"WEBHOOK-{payload['invoice_id']}")
            except urllib.error.HTTPError as exc:
                if exc.code < 500:  # the receiver rejected the data: retrying won't help
                    raise ERPError(f"accounting system rejected the invoice (HTTP {exc.code})") from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(2**attempt)
        raise ERPError(f"accounting system unreachable after {self.retries} attempts: {last_error}")
