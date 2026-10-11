"""Remove personal and banking data from document text before it is sent to an external LLM.

Only fields the pipeline does not need are removed (bank details, emails, phone numbers).
IBANs are only redacted when they pass the real IBAN checksum, so invoice numbers that
merely look similar are left alone.
"""
from __future__ import annotations

import re

_IBAN_CANDIDATE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_PHONE = re.compile(r"(?i)\b(tel(?:efon)?|phone|fax|mobil(?:e)?)\b\.?\s*:?\s*\+?\d[\d\s/().\-]{5,}\d")


def iban_is_valid(value: str) -> bool:
    iban = value.replace(" ", "").upper()
    if not 15 <= len(iban) <= 34 or not iban[:2].isalpha() or not iban[2:4].isdigit():
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def redact_pii(text: str) -> str:
    text = _IBAN_CANDIDATE.sub(lambda m: "[IBAN]" if iban_is_valid(m.group(0)) else m.group(0), text)
    text = _EMAIL.sub("[EMAIL]", text)
    text = _PHONE.sub(lambda m: f"{m.group(1)}: [PHONE]", text)
    return text
