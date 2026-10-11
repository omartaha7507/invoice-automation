from email.message import EmailMessage

from app.email_ingest import save_attachments


def test_only_supported_attachments_are_saved(tmp_path):
    message = EmailMessage()
    message["Subject"] = "Rechnung"
    message.set_content("Anbei unsere Rechnung.")
    message.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="Rechnung 42.pdf")
    message.add_attachment(b"hello", maintype="text", subtype="plain", filename="notes.txt")

    saved = save_attachments(message, tmp_path, prefix="email-7")

    assert [p.name for p in saved] == ["email-7-Rechnung_42.pdf"]
    assert saved[0].read_bytes() == b"%PDF-1.4 fake"
