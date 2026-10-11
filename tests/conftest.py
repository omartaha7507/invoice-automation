import os
import tempfile

# Point the app at a throwaway folder BEFORE it is imported, and never call a real LLM in tests.
_TMP = tempfile.mkdtemp(prefix="invoice-tests-")
os.environ["DATA_DIR"] = _TMP
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["GEMINI_API_KEY"] = ""

import random  # noqa: E402
from datetime import date, timedelta  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import init_db, make_engine  # noqa: E402
from app.erp import MockERPClient  # noqa: E402
from app.pipeline import InvoicePipeline  # noqa: E402
from app.schemas import ExtractedInvoice, LineItemData  # noqa: E402
from evaluation.synth import make_de_vat_id, make_facts, make_supplier  # noqa: E402


class FakeExtractor:
    """Stands in for Gemini: returns whatever the test tells it to, and records the calls."""

    def __init__(self) -> None:
        self.result: ExtractedInvoice | None = None
        self.error: Exception | None = None
        self.calls: list[str] = []

    def _respond(self) -> ExtractedInvoice:
        if self.error:
            raise self.error
        assert self.result is not None, "test forgot to set fake_extractor.result"
        return self.result

    def extract_from_text(self, text: str) -> ExtractedInvoice:
        self.calls.append("text")
        return self._respond()

    def extract_from_file(self, data: bytes, mime_type: str) -> ExtractedInvoice:
        self.calls.append(mime_type)
        return self._respond()


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite://")
    init_db(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def db(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture
def fake_extractor():
    return FakeExtractor()


@pytest.fixture
def pipeline(tmp_path, fake_extractor):
    return InvoicePipeline(fake_extractor, MockERPClient(tmp_path / "outbox"), tmp_path / "docs")


@pytest.fixture
def clean_invoice():
    """A correct invoice, as the extractor would return it."""

    def build(**overrides) -> ExtractedInvoice:
        data = dict(
            supplier_name="Müller Bürobedarf GmbH",
            supplier_vat_id=make_de_vat_id(random.Random(42)),
            buyer_name="Spreewerk Handel GmbH",
            invoice_number="RE-2026-00042",
            invoice_date=date.today() - timedelta(days=5),
            due_date=date.today() + timedelta(days=25),
            currency="EUR",
            net_total=100.00,
            vat_total=19.00,
            gross_total=119.00,
            line_items=[
                LineItemData(description="Toner", quantity=1, unit_price=60.0, line_total=60.0, vat_rate=19),
                LineItemData(description="Paper", quantity=8, unit_price=5.0, line_total=40.0, vat_rate=19),
            ],
        )
        data.update(overrides)
        return ExtractedInvoice(**data)

    return build


@pytest.fixture
def facts():
    """Generator facts for building real PDF/XML test files."""
    rng = random.Random(11)
    supplier = make_supplier(rng, 0)
    return make_facts(rng, supplier, "de", date.today(), 501)
