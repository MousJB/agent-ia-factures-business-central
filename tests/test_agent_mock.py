"""End-to-end tests: offline LLM + MockERP on the 5 generated sample invoices."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.agent.invoice_agent import InvoiceAgent
from app.erp.mock_erp import MockERP
from app.llm.offline_client import OfflineClient
from app.models import InvoiceJob, JobStatus

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


@pytest.fixture()
def erp(tmp_path: Path) -> MockERP:
    shutil.copytree(ROOT / "data" / "seed", tmp_path / "seed")
    return MockERP(tmp_path)


def run(erp: MockERP, filename: str) -> InvoiceJob:
    job = InvoiceJob(id="t", filename=filename, llm_provider="offline", erp_mode="mock")
    return InvoiceAgent(OfflineClient(), erp).process(job, (SAMPLES / filename).read_bytes())


def codes(job: InvoiceJob) -> set[str]:
    return {a.code for a in job.anomalies}


def test_perfect_invoice(erp: MockERP) -> None:
    job = run(erp, "01_facture_conforme_fabrikam.pdf")
    assert job.error is None
    assert job.status == JobStatus.AWAITING_VALIDATION
    assert job.anomalies == []
    assert job.recommendation == "validate"
    assert job.vendor and job.vendor.number == "10000"
    assert job.purchase_order and job.purchase_order.number == "106001"
    assert job.extracted and job.extracted.total_incl_tax == pytest.approx(4318.08)
    assert job.draft_invoice and job.draft_invoice.status == "Draft"


def test_amount_gap(erp: MockERP) -> None:
    job = run(erp, "02_facture_ecart_montant_wwi.pdf")
    assert job.status == JobStatus.AWAITING_VALIDATION
    assert {"AMOUNT_MISMATCH_PO", "PRICE_MISMATCH_PO"} <= codes(job)
    assert job.recommendation == "review"


def test_duplicate(erp: MockERP) -> None:
    job = run(erp, "03_facture_doublon_first_up.pdf")
    assert job.status == JobStatus.BLOCKED
    assert "DUPLICATE_INVOICE" in codes(job)
    assert job.draft_invoice is None
    assert job.recommendation == "reject"


def test_unknown_vendor(erp: MockERP) -> None:
    job = run(erp, "04_facture_fournisseur_inconnu.pdf")
    assert job.status == JobStatus.BLOCKED
    assert "UNKNOWN_VENDOR" in codes(job)
    assert job.draft_invoice is None


def test_inconsistent_total(erp: MockERP) -> None:
    job = run(erp, "05_facture_total_incoherent_gdi.pdf")
    assert "TOTAL_INCONSISTENT" in codes(job)
    assert "AMOUNT_MISMATCH_PO" not in codes(job)  # HT matches the PO, only TTC is wrong
    assert job.recommendation == "review"


def test_validated_invoice_becomes_duplicate(erp: MockERP) -> None:
    first = run(erp, "01_facture_conforme_fabrikam.pdf")
    assert first.draft_invoice
    erp.post_purchase_invoice(first.draft_invoice.id)
    second = run(erp, "01_facture_conforme_fabrikam.pdf")
    assert "DUPLICATE_INVOICE" in codes(second)
    assert second.draft_invoice is None


def test_guardrail_blocks_draft_without_checks(erp: MockERP) -> None:
    from app.agent.tools import InvoiceToolbox

    job = InvoiceJob(id="g", filename="x.pdf", llm_provider="offline", erp_mode="mock")
    toolbox = InvoiceToolbox(job, erp)
    result = toolbox.execute("create_draft_purchase_invoice", {"vendor_number": "10000", "purchase_order_number": None})
    assert "error" in result
    assert toolbox.execute("unknown_tool", {}) == {"error": "Outil inconnu : unknown_tool"}
