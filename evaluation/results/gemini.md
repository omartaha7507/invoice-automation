# Evaluation report (gemini extractor, 10 documents)

| Metric | Value |
|---|---|
| Field-level accuracy | 100.0% |
| Documents fully correct | 100.0% |
| Routing accuracy (approve vs review) | 100.0% |
| False auto-approvals (bad invoice approved) | 0 |
| Unnecessary reviews (good invoice flagged) | 0 |
| Mean / p95 processing time | 19472.3 ms / 35721 ms |

## Accuracy per field

| Field | Accuracy |
|---|---|
| document_type | 100.0% |
| supplier_name | 100.0% |
| supplier_vat_id | 100.0% |
| buyer_name | 100.0% |
| invoice_number | 100.0% |
| invoice_date | 100.0% |
| due_date | 100.0% |
| po_number | 100.0% |
| currency | 100.0% |
| net_total | 100.0% |
| vat_total | 100.0% |
| gross_total | 100.0% |
| line_items | 100.0% |

## Accuracy per input format

| Format | Accuracy |
|---|---|
| pdf_text | 100.0% |
| scan_image | 100.0% |
| xml_cii | 100.0% |
| xml_ubl | 100.0% |

## Issue detection

| Check | Caught | Missed | False alarms | Precision | Recall |
|---|---|---|---|---|---|
| line_items_sum_mismatch | 1 | 0 | 0 | 1.0 | 1.0 |
| po_amount_mismatch | 1 | 0 | 0 | 1.0 | 1.0 |
| totals_mismatch | 1 | 0 | 0 | 1.0 | 1.0 |
