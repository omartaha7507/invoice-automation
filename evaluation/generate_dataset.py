"""Generate a labelled test set of invoices.

    python -m evaluation.generate_dataset --n 60 --seed 7 --out evaluation/dataset

Writes docs/ (the files), ground_truth.json (the correct answers) and purchase_orders.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from evaluation.synth import (
    SCENARIO_ISSUES,
    SUPPLIERS,
    build_cii_xml,
    build_ubl_xml,
    build_zugferd_pdf,
    make_de_vat_id,
    make_facts,
    make_supplier,
    render_pdf,
    render_scan,
)

SCENARIO_WEIGHTS = {
    "clean": 55,
    "line_sum_mismatch": 6,
    "totals_mismatch": 6,
    "invalid_vat_id": 6,
    "duplicate": 6,
    "unknown_po": 6,
    "po_amount_mismatch": 6,
    "delivery_note": 5,
}
CLEAN_FORMATS = {"pdf_text": 50, "xml_cii": 12, "xml_ubl": 10, "pdf_zugferd": 10, "scan_image": 18}
EXTENSIONS = {"pdf_text": "pdf", "pdf_zugferd": "pdf", "xml_cii": "xml", "xml_ubl": "xml", "scan_image": "jpg"}


@dataclass
class GeneratedDoc:
    file: str
    format: str
    scenario: str
    language: str
    template: str
    expected: dict[str, Any]
    expected_status: str
    expected_issues: list[str]
    content: bytes = field(repr=False, default=b"")

    def label(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("content")
        return data


def _pick(rng: random.Random, weights: dict[str, int]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def generate(n: int, seed: int, today: date | None = None) -> tuple[list[GeneratedDoc], list[dict[str, Any]]]:
    rng = random.Random(seed)
    today = today or date.today()
    suppliers = [make_supplier(rng, i) for i in range(len(SUPPLIERS))]
    german = [i for i, s in enumerate(suppliers) if s["country"] == "DE"]
    counters = {i: rng.randint(100, 900) for i in range(len(suppliers))}
    docs: list[GeneratedDoc] = []
    purchase_orders: list[dict[str, Any]] = []
    duplicate_sources: list[tuple] = []

    for index in range(n):
        scenario = _pick(rng, SCENARIO_WEIGHTS)
        if scenario == "duplicate" and not duplicate_sources:
            scenario = "clean"

        if scenario == "duplicate":
            facts, lang, template = rng.choice(duplicate_sources)
            fmt = "pdf_text"
            content = render_pdf(facts, lang, template, copy_mark=True)
        else:
            supplier_index = rng.choice(german) if scenario == "invalid_vat_id" else rng.randrange(len(suppliers))
            supplier = dict(suppliers[supplier_index])
            lang = "de" if supplier["country"] in ("DE", "AT") and rng.random() < 0.8 else "en"
            counters[supplier_index] += 1
            facts = make_facts(rng, supplier, lang, today, counters[supplier_index])
            template = rng.choice(["classic", "banner", "compact"])

            if scenario == "invalid_vat_id":
                supplier["vat_id"] = make_de_vat_id(rng, valid=False)
            elif scenario == "line_sum_mismatch":
                facts.net = round(facts.net + rng.choice([10.0, -5.0, 20.5]), 2)
                facts.gross = round(facts.net + facts.vat, 2)
            elif scenario == "totals_mismatch":
                facts.gross = round(facts.gross + rng.choice([5.0, 1.0, -3.5, 12.4]), 2)
            elif scenario == "delivery_note":
                facts.document_type = "delivery_note"

            # Purchase orders: only clean and PO scenarios carry one, so each document has exactly
            # the problems its scenario says.
            if scenario in ("unknown_po", "po_amount_mismatch") and not facts.po_number:
                facts.po_number = f"PO-{rng.randint(100000, 999999)}"
            if scenario not in ("clean", "unknown_po", "po_amount_mismatch"):
                facts.po_number = None
            if facts.po_number and scenario != "unknown_po":
                amount = facts.gross if scenario == "clean" else round(facts.gross * 1.15, 2)
                purchase_orders.append(
                    {"po_number": facts.po_number, "supplier_name": supplier["name"], "amount": amount, "currency": "EUR"}
                )

            fmt = _pick(rng, CLEAN_FORMATS) if scenario == "clean" else ("pdf_text" if rng.random() < 0.7 else "scan_image")
            if fmt == "xml_cii":
                content = build_cii_xml(facts)
            elif fmt == "xml_ubl":
                content = build_ubl_xml(facts)
            elif fmt == "pdf_zugferd":
                content = build_zugferd_pdf(facts, lang, template)
            else:
                content = render_pdf(facts, lang, template)
                if fmt == "scan_image":
                    content = render_scan(content, rng)
            if scenario == "clean" and fmt in ("pdf_text", "scan_image"):
                duplicate_sources.append((facts, lang, template))

        issues = SCENARIO_ISSUES[scenario]
        docs.append(
            GeneratedDoc(
                file=f"doc_{index + 1:03d}.{EXTENSIONS[fmt]}",
                format=fmt,
                scenario=scenario,
                language=lang,
                template=template,
                expected=facts.expected(),
                expected_status="approved" if not issues else "needs_review",
                expected_issues=list(issues),
                content=content,
            )
        )
    return docs, purchase_orders


def write_dataset(out_dir: Path, n: int, seed: int) -> list[GeneratedDoc]:
    docs, purchase_orders = generate(n, seed)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "docs").mkdir(parents=True)
    for doc in docs:
        (out_dir / "docs" / doc.file).write_bytes(doc.content)
    (out_dir / "ground_truth.json").write_text(
        json.dumps({"seed": seed, "count": n, "documents": [d.label() for d in docs]}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    with (out_dir / "purchase_orders.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["po_number", "supplier_name", "amount", "currency"])
        writer.writeheader()
        writer.writerows(purchase_orders)
    return docs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n", type=int, default=60, help="number of documents")
    parser.add_argument("--seed", type=int, default=7, help="change it to get a different set")
    parser.add_argument("--out", type=Path, default=Path("evaluation/dataset"))
    args = parser.parse_args()
    docs = write_dataset(args.out, args.n, args.seed)
    by_scenario: dict[str, int] = {}
    for doc in docs:
        by_scenario[doc.scenario] = by_scenario.get(doc.scenario, 0) + 1
    print(f"Wrote {len(docs)} documents to {args.out}: {by_scenario}")


if __name__ == "__main__":
    main()
