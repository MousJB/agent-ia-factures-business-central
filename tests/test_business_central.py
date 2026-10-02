"""BusinessCentralERP against a fake BC API v2.0 (httpx.MockTransport)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.agent.invoice_agent import InvoiceAgent
from app.erp.business_central import BusinessCentralERP
from app.llm.offline_client import OfflineClient
from app.models import InvoiceJob, JobStatus

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
COMPANY = "c0ffee00-0000-0000-0000-000000000001"


class FakeBC:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.filters: list[str] = []
        self.invoices: dict[str, dict[str, Any]] = {}
        self.lines: dict[str, list[dict[str, Any]]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if "oauth2/v2.0/token" in path:
            assert b"grant_type=client_credentials" in request.content
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer tok"
        flt = request.url.params.get("$filter", "")
        self.filters.append(flt)

        if path.endswith("/companies"):
            return httpx.Response(200, json={"value": [{"id": COMPANY, "name": "CRONUS France S.A."}]})
        if path.endswith("/vendors"):
            vendor = {"id": "v1", "number": "10000", "displayName": "Fabrikam, Inc.",
                      "taxRegistrationNumber": "FR40123456789", "city": "Paris", "currencyCode": "", "blocked": " "}
            if "taxRegistrationNumber" in flt and "FR40123456789" not in flt:
                return httpx.Response(200, json={"value": []})
            return httpx.Response(200, json={"value": [vendor]})
        if path.endswith("/purchaseOrders"):
            assert request.url.params.get("$expand") == "purchaseOrderLines"
            return httpx.Response(200, json={"value": [{
                "id": "po1", "number": "106001", "vendorNumber": "10000", "vendorName": "Fabrikam, Inc.",
                "orderDate": "2026-09-08", "status": "Open",
                "totalAmountExcludingTax": 3598.40, "totalAmountIncludingTax": 4318.08,
                "purchaseOrderLines": [
                    {"lineType": "Item", "lineObjectNumber": "1896-S", "description": "ATHENS Desk", "quantity": 4, "directUnitCost": 649.40},
                    {"lineType": "Item", "lineObjectNumber": "1900-S", "description": "PARIS Guest Chair, black", "quantity": 8, "directUnitCost": 125.10},
                ],
            }]})
        if path.endswith("/purchaseInvoices") and method == "GET":
            assert "vendorInvoiceNumber eq 'FAB-2026-0412'" in flt
            matches = [i for i in self.invoices.values() if i["vendorInvoiceNumber"] == "FAB-2026-0412"]
            return httpx.Response(200, json={"value": matches})
        if path.endswith("/purchaseInvoices") and method == "POST":
            body = json.loads(request.content)
            inv = {"id": "pi1", "number": "108001", "status": "Draft", **body}
            self.invoices["pi1"] = inv
            return httpx.Response(201, json=inv)
        if path.endswith("/purchaseInvoiceLines") and method == "POST":
            self.lines.setdefault("pi1", []).append(json.loads(request.content))
            return httpx.Response(201, json={"id": "l"})
        if path.endswith("purchaseInvoices(pi1)/Microsoft.NAV.post"):
            self.invoices["pi1"]["status"] = "Open"
            return httpx.Response(204)
        if path.endswith("purchaseInvoices(pi1)") and method == "GET":
            return httpx.Response(200, json=self.invoices["pi1"])
        if path.endswith("purchaseInvoices(pi1)") and method == "DELETE":
            assert request.headers.get("If-Match") == "*"
            del self.invoices["pi1"]
            return httpx.Response(204)
        return httpx.Response(404, json={"error": {"code": "NotFound", "message": path}})


@pytest.fixture()
def fake() -> FakeBC:
    return FakeBC()


@pytest.fixture()
def erp(fake: FakeBC) -> BusinessCentralERP:
    return BusinessCentralERP(
        tenant_id="t", client_id="c", client_secret="s", environment="Sandbox",
        company_name="CRONUS France S.A.", http=httpx.Client(transport=httpx.MockTransport(fake.handler)),
    )


def test_full_flow_on_business_central(erp: BusinessCentralERP, fake: FakeBC) -> None:
    job = InvoiceJob(id="bc", filename="f.pdf", llm_provider="offline", erp_mode="business_central")
    InvoiceAgent(OfflineClient(), erp).process(job, (SAMPLES / "01_facture_conforme_fabrikam.pdf").read_bytes())

    assert job.error is None, job.error
    assert job.status == JobStatus.AWAITING_VALIDATION
    assert job.anomalies == []
    assert job.draft_invoice and job.draft_invoice.status == "Draft"
    assert [line["lineObjectNumber"] for line in fake.lines["pi1"]] == ["1896-S", "1900-S"]
    assert fake.invoices["pi1"]["vendorInvoiceNumber"] == "FAB-2026-0412"
    assert sum(1 for m, p in fake.calls if "token" in p) == 1  # token cached

    posted = erp.post_purchase_invoice("pi1")
    assert posted.status == "Open"


def test_duplicate_detected_after_creation(erp: BusinessCentralERP, fake: FakeBC) -> None:
    fake.invoices["pi1"] = {"id": "pi1", "number": "108001", "status": "Open", "vendorNumber": "10000",
                            "vendorInvoiceNumber": "FAB-2026-0412", "invoiceDate": "2026-09-18"}
    assert erp.find_purchase_invoices("10000", "FAB-2026-0412")[0].status == "Open"


def test_bc_error_is_surfaced(erp: BusinessCentralERP) -> None:
    from app.erp.base import ERPError

    with pytest.raises(ERPError, match="NotFound"):
        erp.delete_draft_purchase_invoice("unknown")


def test_odata_quotes_are_escaped(erp: BusinessCentralERP, fake: FakeBC) -> None:
    erp.search_vendors(name="L'Atelier", vat_number="FR'99")
    assert "taxRegistrationNumber eq 'FR''99'" in fake.filters
