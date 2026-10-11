"""Values shared across the app, kept in one place so they can't drift apart."""

STATUS_APPROVED = "approved"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_REJECTED = "rejected"
ALL_STATUSES = (STATUS_APPROVED, STATUS_NEEDS_REVIEW, STATUS_REJECTED)

# How the data was obtained from the document.
METHOD_XML = "xml"                    # a pure XRechnung / ZUGFeRD XML file
METHOD_XML_EMBEDDED = "xml_embedded"  # a ZUGFeRD PDF with the XML attached inside
METHOD_LLM_TEXT = "llm_text"          # a digital PDF: text extracted, redacted, sent to the LLM
METHOD_LLM_VISION = "llm_vision"      # a scan or photo: the image is sent to the LLM
METHOD_FAILED = "failed"              # nothing could be read; a human enters the data

SUPPORTED_TYPES = {
    ".pdf": "application/pdf",
    ".xml": "application/xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
