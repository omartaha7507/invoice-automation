import random

from app.redaction import iban_is_valid, redact_pii
from evaluation.synth import make_iban


def test_valid_iban_is_removed():
    iban = make_iban(random.Random(1))
    assert iban_is_valid(iban)
    assert redact_pii(f"IBAN: {iban}") == "IBAN: [IBAN]"


def test_lookalike_numbers_are_kept():
    text = "Rechnung RE20260001234567, USt-IdNr. DE129274202"
    assert redact_pii(text) == text


def test_email_and_phone_are_removed():
    redacted = redact_pii("Tel. +49 30 1234567 | E-Mail: buchhaltung@firma.de")
    assert "1234567" not in redacted and "firma.de" not in redacted
    assert "[PHONE]" in redacted and "[EMAIL]" in redacted
