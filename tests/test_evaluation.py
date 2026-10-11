"""End-to-end: generate a labelled dataset and run the whole pipeline on it with a perfect reader.
With correct extraction, every document must be routed exactly as its label says."""
from evaluation.generate_dataset import write_dataset
from evaluation.run_eval import evaluate, field_matches


def test_pipeline_routes_every_document_correctly(tmp_path):
    write_dataset(tmp_path / "ds", n=30, seed=5)
    summary = evaluate(tmp_path / "ds", "oracle")
    assert summary["documents"] == 30
    assert summary["field_accuracy"] == 1.0
    assert summary["routing_accuracy"] == 1.0
    assert summary["false_approvals"] == []
    for stats in summary["issue_detection"].values():
        assert stats["fp"] == 0 and stats["fn"] == 0


def test_field_comparison_is_forgiving_about_format_only():
    assert field_matches("supplier_name", "Müller GmbH", "MÜLLER GMBH")
    assert field_matches("gross_total", 119.0, 119.004)
    assert not field_matches("gross_total", 119.0, 119.1)
    assert field_matches("supplier_vat_id", "DE129274202", "DE 129 274 202")
    assert not field_matches("po_number", None, "PO-1")
