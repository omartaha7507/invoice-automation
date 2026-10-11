# How it works: a guide for explaining this project

This guide explains the whole project in plain language. Read it before interviews. Each section says
*what* a part does, *why* it was built that way, and *where* it is in the code.

---

## 1. The problem, in two sentences

Companies receive hundreds of invoices a month as PDFs, scans and, more and more, as German e-invoices
(XRechnung, ZUGFeRD). Someone has to read each one, type the data into the accounting system, and catch
mistakes like wrong totals or duplicate bills. This project automates that, and keeps a human in charge of
the cases that need judgement.

**The 30-second pitch:**
> "I built an invoice processing pipeline for German companies. It reads PDFs, scans and XML e-invoices,
> uses an LLM only where it's needed, then checks the result with business rules like totals, VAT ID check
> digits, duplicates and purchase-order matching. Clean invoices go to accounting automatically. Problem
> cases go to a review screen with the reasons highlighted. I measured it on a labelled test set, and CI
> blocks any change that would let a faulty invoice be approved automatically."

---

## 2. The journey of one invoice

Follow one file through the system. This is the best way to explain the architecture.

**Step 1: The file arrives.** It comes through the web upload (`POST /api/documents` in `app/api.py`), or
it lands in the `inbox/` folder or an email inbox. The worker in `app/watcher.py` checks those every 30 seconds.

**Step 2: Have we seen this exact file before?** The pipeline computes a SHA-256 hash of the bytes. That's
a fingerprint: identical files give identical hashes. If the hash is already in the database, nothing is
processed twice (`pipeline.py`, `process`).

**Step 3: The original is stored.** It's saved under its hash, so the reviewer can always open it, and
because bookkeeping rules require keeping originals.

**Step 4: Choose how to read it.** `extract_document` in `pipeline.py` takes the cheapest reliable route:

| What the file is | How it's read | Why |
|---|---|---|
| XML e-invoice | Exact XML parser | Already structured data. No AI needed, 100% accurate, free |
| PDF with XML attached (ZUGFeRD) | Pull out the XML, then the parser | Same as above |
| Normal digital PDF | Extract the text, remove personal data, send text to Gemini | Text is cheaper and more private than images |
| Scan or photo | Send the image to Gemini | No text layer exists, so the model has to "look" |

**Step 5: Check the data.** `validate_invoice` in `app/validation.py` runs the business rules and returns
a list of *issues*. Each issue has a code, a severity (error or warning), a message for humans, and the
field it concerns.

**Step 6: Decide.** `decide_status`: no issues means *approved*; any issue means *needs_review*. Errors
always go to a human. Warnings can be allowed through with a setting, but the default is cautious.

**Step 7: Save and log.** The invoice and its line items are saved. An entry is written to the audit log.

**Step 8: Export.** Approved invoices are sent to the accounting system (`app/erp.py`). If that fails, the
invoice goes back to review with an `erp_export_failed` issue. Nothing is silently lost.

**Step 9: Human review.** In the UI, the reviewer sees the original document next to the extracted fields.
The fields with problems are highlighted. The reviewer corrects, approves or rejects. Corrections are logged
with a before/after "diff". After corrections, the rules run again.

---

## 3. The parts, one by one

### The XML e-invoice parser (`app/extraction/xml_einvoice.py`)
Germany uses the European e-invoice standard (EN 16931). It has two XML "languages":
- **UBL**, used by XRechnung.
- **CII**, used by ZUGFeRD / Factur-X, and also allowed for XRechnung.

The parser reads both, using XPath-like paths such as `cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount`.

It uses **defusedxml** instead of the normal XML library. XML from outside is untrusted. A crafted file can
contain "entity expansion" (an *XML bomb*) that eats all memory, and defusedxml refuses those. There's a test
for this: `test_xml_bomb_is_refused`.

### The LLM extractor (`app/extraction/llm.py`)
- **Structured output:** Gemini is given the `LLMInvoice` schema and must answer in exactly that JSON shape.
- **It's still validated:** the answer is re-checked with Pydantic. An LLM can still return nonsense, so
  it's never trusted blindly.
- **The prompt says "don't fix mistakes":** if the invoice has a wrong total, the model must copy the
  wrong total. If it "helpfully" corrected it, the validation rules couldn't catch the supplier's error.
- **Temperature 0:** you want the same answer every time, not creativity.
- **Rate limiter:** the free tier allows only a few requests per minute. The limiter spaces out calls.
- **Retries with exponential backoff:** on rate-limit (429) or server errors (5xx), or a malformed answer,
  it waits 2, 4, 8… seconds and tries again. On errors like a wrong API key (401/403), it stops at once,
  because retrying can't help.
- **Interface (`Extractor` in `extraction/base.py`):** the pipeline only knows "something with
  `extract_from_text` and `extract_from_file`". That's why the tests can use a fake extractor, and why
  Gemini could be swapped for a local model without touching the pipeline.

### PII redaction (`app/redaction.py`)
Before text goes to an external AI service, it removes IBANs, emails and phone numbers. The pipeline doesn't
need them, and GDPR says to minimise what you share. Clever detail: an IBAN is only removed if it passes the
real **IBAN checksum (mod 97)**. So an invoice number like `RE20260001234567`, which looks a bit like an
IBAN, is not destroyed.

Honest limitation: scans are images, so they can't be redacted this way.

### The validation rules (`app/validation.py`)
These are **pure functions**: no database, no network. You give them data, they return issues. That makes
them easy to test. The pipeline does the lookups (duplicate? purchase order?) and passes the answers in.

| Rule | Severity | Why a company cares |
|---|---|---|
| Required fields present | error | Can't book without supplier, number, date, amount |
| Line items add up to net total | error | Catches calculation errors and misreads |
| Net + VAT = gross total | error | Same |
| VAT ID valid (format + German check digit) | error | A fake or mistyped VAT ID can cost the right to deduct VAT |
| Duplicate invoice (same number + same supplier) | error | Paying a bill twice is a classic, expensive mistake |
| Not an invoice (e.g. delivery note) | error | Shouldn't be booked as a payable |
| Purchase order unknown / amount differs / other supplier | warning | "3-way match": was this actually ordered, at this price? |
| Unusual VAT rate (not 0, 7, 19%) | warning | Probably a misread or a wrong invoice |
| Missing VAT ID over 250 € | warning | German rules: small invoices up to 250 € have fewer mandatory fields |
| Future date, due before issue date, foreign currency | warning | Unusual; worth a look |

**The German VAT ID check digit:** the last digit of a German VAT ID (e.g. DE129274202) is calculated from
the first eight with an official algorithm (ISO 7064, MOD 11,10). The code computes it and compares. The
test uses real companies' VAT IDs (Siemens, SAP) to prove the algorithm is right.

**Money tolerance:** amounts are compared with a tolerance of 2 cents, slightly more for many line items.
Each line can be rounded differently, so demanding exact equality would flag correct invoices.

### The pipeline (`app/pipeline.py`)
This file ties everything together. Points worth mentioning:
- **"Never lose a document."** If extraction crashes, the error is caught, the invoice is saved anyway as
  `needs_review`, and a person can type it in.
- **`flush()` vs `commit()`:** `flush` sends the new row to the database inside the open transaction so it
  gets an ID. The ERP export needs that ID. `commit` makes everything permanent at the end, all together.
- **Approve re-runs the rules.** After a reviewer corrects data, the checks run again. Any issue still there
  is stored as "overridden" in the audit log. A human made that decision knowingly, and it's traceable.

### The database (`app/models.py`)
Four tables:
- `invoices`: one row per document.
- `line_items`: **many rows per invoice**, linked by `invoice_id`. This is a **one-to-many relationship**.
  An invoice can have 1 or 50 items, and a table can't have a flexible number of columns, so the items get
  their own table.
- `purchase_orders`: reference data the invoices are matched against.
- `audit_log`: who did what, when, with details. German bookkeeping rules (GoBD) expect changes to be
  traceable.

**Money is stored as `Numeric(12, 2)`, not `Float`.** Floats can't represent amounts like 0.10 exactly.
Accountants need cents to be exact.

The `issues` column is JSON, because its shape varies and it's always read together with the invoice.

Locally it runs on SQLite; in Docker it runs on PostgreSQL. SQLAlchemy hides the difference. Only the
`DATABASE_URL` changes.

### The export to accounting (`app/erp.py`)
There's no real SAP or DATEV, so `MockERPClient` writes each booking as one line of JSON into a daily
"outbox" file. `WebhookERPClient` can POST to any URL, for example an n8n or Zapier workflow, or a real ERP API.

It sends an **Idempotency-Key** header. If the request times out and is retried, the receiving system can
recognise the repeat and not book the invoice twice. It also only retries on server errors (5xx) and network
problems, not when the receiver says the data is wrong (4xx).

### The API (`app/api.py`)
FastAPI with Pydantic response models. A few deliberate choices:
- **The upload endpoint is a normal `def`, not `async def`.** The LLM call is slow, blocking code. FastAPI
  runs plain `def` endpoints in a thread pool, so one slow upload doesn't freeze the server for everyone.
- The CSV export uses `;` as separator, decimal commas and a BOM marker. That's what German Excel needs to
  open the file correctly with umlauts and amounts.
- Correct HTTP status codes: 404 not found, 409 conflict (e.g. approving twice), 422 invalid input.

### The workers (`app/watcher.py`, `app/email_ingest.py`)
The worker polls the inbox folder and, if configured, an IMAP email inbox. It only takes supported
attachments (PDF, XML, PNG, JPG). It skips files modified in the last 2 seconds, in case a file is still
being copied in. Processed files move to `processed/`, broken ones to `failed/`.

### Docker (`Dockerfile`, `docker-compose.yml`)
Three services:
- `db`: PostgreSQL.
- `api`: the web server and UI.
- `watcher`: the background worker.

The image installs dependencies before copying the code, so rebuilds after code changes are fast (layer
caching). The container runs as a non-root user, and a health check pings `/health`. The worker waits until
the API is healthy, so they don't both create the tables at the same time.

---

## 4. How quality is measured (`evaluation/`)

This is often the most impressive part for interviewers, because most AI demos skip it.

1. **`synth.py` generates realistic invoices** with known correct answers ("ground truth"). The set covers
   10 suppliers, German and English, 3 layouts, and 5 formats. Some invoices contain deliberate errors:
   wrong totals, fake VAT IDs, duplicates, unknown purchase orders, delivery notes.
2. **`run_eval.py` runs the real pipeline** on all of them and compares the results with the truth:
   - accuracy per field and per format
   - how often the approve/review decision was right
   - per check, how many problems were caught or missed, and how many false alarms there were (precision
     and recall)
3. **The most important number is "false auto-approvals"**: a faulty invoice that slipped through
   automatically. A good invoice wrongly sent to review costs a minute of someone's time. A bad invoice
   paid automatically costs real money. The system is designed around that asymmetry.
4. **The oracle mode** replaces the LLM with a perfect reader. That tests everything *except* the LLM.
   With perfect input, routing must be 100% correct. CI runs this on every push. If someone breaks a rule,
   the build fails.
5. The real Gemini evaluation runs manually (`workflow_dispatch`), so the free quota isn't burned on every push.

**Why synthetic data?** Real invoices contain personal data and aren't freely available. Synthetic data
also gives exact labels for free. The honest limitation: real invoices are messier. The next step would be
a pilot on real ones.

---

## 5. Likely interview questions, with answers

**Why not just let the LLM do everything?**
LLMs are good at reading messy documents but can make mistakes and can't be fully trusted. So the LLM
only *reads*. Deterministic rules *decide*. And structured XML invoices skip the LLM entirely, because
parsing is exact and free.

**How do you handle hallucinations?**
In four layers:
1. A strict output schema.
2. Pydantic validation of the answer.
3. A prompt that forbids guessing or correcting values.
4. Business rules that cross-check the numbers against each other.

A made-up total almost never also satisfies "line items = net" and "net + VAT = gross". Anything that fails
goes to a human.

**What happens when Gemini is down or rate-limited?**
The pipeline retries with exponential backoff. If it still fails, the document is saved as
`needs_review`, so nothing is lost. The worker keeps running.

**How would you scale this to 100,000 invoices a month?**
1. Put a queue (e.g. Redis with Celery, or a cloud queue) between upload and processing, and run several
   workers.
2. Move document storage to object storage (S3).
3. Move to a paid LLM tier with higher limits.
4. Raise the share of XML e-invoices, which need no LLM at all; this is happening anyway because of the
   German mandate.

**Why PostgreSQL in production but SQLite locally?**
SQLite needs no setup, which is great for development and tests. PostgreSQL handles many simultaneous
writers, here the API plus the worker. SQLAlchemy makes switching a one-line config change.

**How do you prevent paying an invoice twice?**
1. The same file is caught by its hash.
2. The same invoice as a different file (e.g. a scan of the PDF) is caught by "same invoice number from
   the same supplier".
3. The export to accounting carries an idempotency key.

**What about data protection (GDPR)?**
Text is redacted before leaving the system. Only needed fields are extracted. Originals stay on our own
storage. For stricter needs, a self-hosted model can replace Gemini behind the same interface.

**What would you improve next?**
1. Learn from corrections: if reviewers keep fixing the same field for one supplier, use that.
2. A real DATEV or Lexoffice connector.
3. Track accuracy over time on a dashboard.
4. Test on real invoices.

**What was the hardest part?**
Pick something real from your own experience building and running it. Good candidates:
- Telling the LLM *not* to correct errors.
- Choosing tolerances for rounding.
- Deciding what should be an error and what a warning.

---

## 6. Glossary

| Term | Meaning |
|---|---|
| Accounts payable (AP) | The department/process that pays the company's bills |
| ERP | Enterprise Resource Planning: the company's central business software (SAP, DATEV, etc.) |
| XRechnung | Germany's e-invoice standard (XML), mandatory for invoicing public authorities |
| ZUGFeRD / Factur-X | Hybrid e-invoice: a readable PDF with the XML data attached inside |
| UBL / CII | The two XML formats allowed by the European e-invoice standard EN 16931 |
| USt-IdNr. | German VAT identification number, e.g. DE129274202 |
| GoBD | German rules for proper digital bookkeeping, including traceability of changes |
| 3-way match | Checking invoice vs. purchase order (vs. goods receipt) before paying |
| PII | Personally identifiable information |
| Idempotent | Doing it twice has the same effect as doing it once |
| Structured output | Forcing an LLM to answer in a fixed JSON schema |
| Ground truth | The known correct answer used to score a system |
| Precision / recall | Of the alarms raised, how many were real / of the real problems, how many were caught |
| Human in the loop | People approve or correct what the automation isn't sure about |
