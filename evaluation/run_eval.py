"""Measure how well the pipeline works on a labelled dataset.

    python -m evaluation.run_eval --dataset evaluation/dataset             # real Gemini calls
    python -m evaluation.run_eval --dataset evaluation/dataset --extractor oracle

The "oracle" extractor returns the correct answer for every document that needs the LLM.
That isolates everything else (XML parsing, validation rules, routing): with a perfect reader,
routing must be 100% correct, which is what CI checks on every commit.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import init_db, make_engine
from app.erp import MockERPClient
from app.factory import build_extractor
from app.pipeline import InvoicePipeline, invoice_to_extracted, load_purchase_orders
from app.schemas import ExtractedInvoice

FIELDS = [
    "document_type", "supplier_name", "supplier_vat_id", "buyer_name", "invoice_number", "invoice_date",
    "due_date", "po_number", "currency", "net_total", "vat_total", "gross_total",
]


class OracleExtractor:
    """Returns the ground truth for the document currently being processed."""

    def __init__(self) -> None:
        self.current: Optional[dict[str, Any]] = None

    def _answer(self) -> ExtractedInvoice:
        assert self.current is not None
        return ExtractedInvoice.model_validate(self.current)

    def extract_from_text(self, text: str) -> ExtractedInvoice:
        return self._answer()

    def extract_from_file(self, data: bytes, mime_type: str) -> ExtractedInvoice:
        return self._answer()


def _norm_text(value: Any) -> str:
    return re.sub(r"[^0-9a-zäöüß]+", "", str(value).casefold()) if value else ""


def field_matches(field: str, expected: Any, actual: Any) -> bool:
    if expected in (None, ""):
        return actual in (None, "")
    if actual in (None, ""):
        return False
    if field.endswith("_total"):
        return abs(float(expected) - float(actual)) <= 0.011
    if field.endswith("_date"):
        return str(actual)[:10] == str(expected)[:10]
    if field in ("supplier_vat_id", "invoice_number", "po_number", "currency"):
        return re.sub(r"\s", "", str(expected)).upper() == re.sub(r"\s", "", str(actual)).upper()
    return _norm_text(expected) == _norm_text(actual)


def line_items_match(expected: list[dict], actual: list[dict]) -> bool:
    if len(expected) != len(actual):
        return False
    exp = sorted(round(float(i["line_total"] or 0), 2) for i in expected)
    act = sorted(round(float(i["line_total"] or 0), 2) for i in actual)
    return all(abs(a - b) <= 0.011 for a, b in zip(exp, act))


def evaluate(dataset_dir: Path, extractor_name: str = "gemini", limit: Optional[int] = None) -> dict[str, Any]:
    truth = json.loads((dataset_dir / "ground_truth.json").read_text(encoding="utf-8"))
    documents = truth["documents"][:limit] if limit else truth["documents"]

    if extractor_name == "oracle":
        extractor = OracleExtractor()
    else:
        if not settings.gemini_api_key:
            raise SystemExit("Set GEMINI_API_KEY (or use --extractor oracle).")
        extractor = build_extractor(settings)

    records = []
    with tempfile.TemporaryDirectory() as tmp:
        engine = make_engine(f"sqlite:///{tmp}/eval.db")
        init_db(engine)
        Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        with Session() as db:
            load_purchase_orders(db, dataset_dir / "purchase_orders.csv")
        pipeline = InvoicePipeline(
            extractor, MockERPClient(Path(tmp) / "outbox"), Path(tmp) / "docs", redact_pii=settings.redact_pii
        )

        for number, doc in enumerate(documents, start=1):
            if isinstance(extractor, OracleExtractor):
                extractor.current = doc["expected"]
            data = (dataset_dir / "docs" / doc["file"]).read_bytes()
            with Session() as db:
                invoice, _ = pipeline.process(db, data, doc["file"], source="eval")
                actual = invoice_to_extracted(invoice).model_dump(mode="json")
                records.append(
                    {
                        "file": doc["file"], "format": doc["format"], "scenario": doc["scenario"],
                        "method": invoice.extraction_method, "status": invoice.status,
                        "expected_status": doc["expected_status"],
                        "issues": sorted({i["code"] for i in invoice.issues}),
                        "expected_issues": sorted(doc["expected_issues"]),
                        "processing_ms": invoice.processing_ms or 0,
                        "expected": doc["expected"], "actual": actual,
                    }
                )
            print(f"[{number}/{len(documents)}] {doc['file']}: {invoice.status} via {invoice.extraction_method}")

        # close the database connection so Windows can delete the temp folder
        engine.dispose()

    return summarise(records, extractor_name)


def summarise(records: list[dict[str, Any]], extractor_name: str) -> dict[str, Any]:
    field_counts = {f: [0, 0] for f in FIELDS + ["line_items"]}
    by_format: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    mistakes: list[dict[str, Any]] = []
    exact_docs = scored_docs = 0

    for r in records:
        fields = ["document_type"] if r["expected"]["document_type"] == "delivery_note" else FIELDS + ["line_items"]
        all_ok = True
        for f in fields:
            if f == "line_items":
                ok = line_items_match(r["expected"]["line_items"], r["actual"]["line_items"])
            else:
                ok = field_matches(f, r["expected"][f], r["actual"][f])
            field_counts[f][0] += ok
            field_counts[f][1] += 1
            by_format[r["format"]][0] += ok
            by_format[r["format"]][1] += 1
            if not ok:
                all_ok = False
                mistakes.append({"file": r["file"], "field": f, "expected": r["expected"].get(f), "actual": r["actual"].get(f)})
        scored_docs += 1
        exact_docs += all_ok

    routing_ok = sum(r["status"] == r["expected_status"] for r in records)
    false_approvals = [r["file"] for r in records if r["expected_status"] == "needs_review" and r["status"] == "approved"]
    unnecessary_reviews = [r["file"] for r in records if r["expected_status"] == "approved" and r["status"] != "approved"]

    issue_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    for r in records:
        expected, actual = set(r["expected_issues"]), set(r["issues"])
        for code in expected & actual:
            issue_stats[code]["tp"] += 1
        for code in actual - expected:
            issue_stats[code]["fp"] += 1
        for code in expected - actual:
            issue_stats[code]["fn"] += 1

    total_correct = sum(c for c, _ in field_counts.values())
    total_fields = sum(t for _, t in field_counts.values())
    latencies = [r["processing_ms"] for r in records]
    return {
        "extractor": extractor_name,
        "documents": len(records),
        "field_accuracy": round(total_correct / total_fields, 4) if total_fields else 0.0,
        "document_exact_match": round(exact_docs / scored_docs, 4) if scored_docs else 0.0,
        "routing_accuracy": round(routing_ok / len(records), 4) if records else 0.0,
        "false_approvals": false_approvals,
        "unnecessary_reviews": unnecessary_reviews,
        "per_field": {f: round(c / t, 4) for f, (c, t) in field_counts.items() if t},
        "per_format": {f: round(c / t, 4) for f, (c, t) in by_format.items() if t},
        "issue_detection": {
            code: {
                **s,
                "precision": round(s["tp"] / (s["tp"] + s["fp"]), 3) if s["tp"] + s["fp"] else None,
                "recall": round(s["tp"] / (s["tp"] + s["fn"]), 3) if s["tp"] + s["fn"] else None,
            }
            for code, s in sorted(issue_stats.items())
        },
        "latency_ms": {
            "mean": round(statistics.mean(latencies), 1) if latencies else 0,
            "p95": sorted(latencies)[int(0.95 * (len(latencies) - 1))] if latencies else 0,
        },
        "methods": {m: sum(r["method"] == m for r in records) for m in sorted({r["method"] for r in records})},
        "mistakes": mistakes[:50],
    }


def to_markdown(s: dict[str, Any]) -> str:
    pct = lambda v: f"{v * 100:.1f}%"  # noqa: E731
    lines = [
        f"# Evaluation report ({s['extractor']} extractor, {s['documents']} documents)",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Field-level accuracy | {pct(s['field_accuracy'])} |",
        f"| Documents fully correct | {pct(s['document_exact_match'])} |",
        f"| Routing accuracy (approve vs review) | {pct(s['routing_accuracy'])} |",
        f"| False auto-approvals (bad invoice approved) | {len(s['false_approvals'])} |",
        f"| Unnecessary reviews (good invoice flagged) | {len(s['unnecessary_reviews'])} |",
        f"| Mean / p95 processing time | {s['latency_ms']['mean']} ms / {s['latency_ms']['p95']} ms |",
        "",
        "## Accuracy per field",
        "",
        "| Field | Accuracy |",
        "|---|---|",
        *[f"| {f} | {pct(v)} |" for f, v in s["per_field"].items()],
        "",
        "## Accuracy per input format",
        "",
        "| Format | Accuracy |",
        "|---|---|",
        *[f"| {f} | {pct(v)} |" for f, v in sorted(s["per_format"].items())],
        "",
        "## Issue detection",
        "",
        "| Check | Caught | Missed | False alarms | Precision | Recall |",
        "|---|---|---|---|---|---|",
        *[
            f"| {code} | {v['tp']} | {v['fn']} | {v['fp']} | {v['precision'] if v['precision'] is not None else '-'} | "
            f"{v['recall'] if v['recall'] is not None else '-'} |"
            for code, v in s["issue_detection"].items()
        ],
    ]
    if s["mistakes"]:
        lines += ["", "## Extraction mistakes (first 50)", "", "| File | Field | Expected | Got |", "|---|---|---|---|"]
        lines += [f"| {m['file']} | {m['field']} | {m['expected']} | {m['actual']} |" for m in s["mistakes"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, default=Path("evaluation/dataset"))
    parser.add_argument("--extractor", choices=["gemini", "oracle"], default="gemini")
    parser.add_argument("--limit", type=int, help="only evaluate the first N documents")
    parser.add_argument("--out", type=Path, default=Path("evaluation/results"))
    parser.add_argument("--min-field-accuracy", type=float, default=0.0)
    parser.add_argument("--min-routing-accuracy", type=float, default=0.0)
    parser.add_argument("--max-false-approvals", type=int, default=None)
    args = parser.parse_args()

    summary = evaluate(args.dataset, args.extractor, args.limit)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{args.extractor}.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    report = to_markdown(summary)
    (args.out / f"{args.extractor}.md").write_text(report, encoding="utf-8")
    print("\n" + report)

    failures = []
    if summary["field_accuracy"] < args.min_field_accuracy:
        failures.append(f"field accuracy {summary['field_accuracy']} < {args.min_field_accuracy}")
    if summary["routing_accuracy"] < args.min_routing_accuracy:
        failures.append(f"routing accuracy {summary['routing_accuracy']} < {args.min_routing_accuracy}")
    if args.max_false_approvals is not None and len(summary["false_approvals"]) > args.max_false_approvals:
        failures.append(f"{len(summary['false_approvals'])} false approvals > {args.max_false_approvals}")
    if failures:
        print("QUALITY GATE FAILED: " + "; ".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()
