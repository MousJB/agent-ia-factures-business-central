"""Microsoft Dynamics 365 Business Central implementation (API v2.0, OAuth2 client credentials).

Prerequisites (see README):
  * Entra ID app registration with application permission
    "Dynamics 365 Business Central / API.ReadWrite.All" + admin consent;
  * the app declared in Business Central ("Microsoft Entra Applications" page)
    with permission sets allowing purchase documents (e.g. D365 BUS FULL ACCESS).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date
from typing import Any

import httpx

from app.config import Settings
from app.erp.base import ERPClient, ERPError, rank_vendors
from app.models import (
    DraftInvoiceRequest,
    PurchaseInvoiceSummary,
    PurchaseOrder,
    PurchaseOrderLine,
    Vendor,
)

logger = logging.getLogger(__name__)

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
API_ROOT = "https://api.businesscentral.dynamics.com/v2.0/{tenant}/{environment}/api/v2.0"
SCOPE = "https://api.businesscentral.dynamics.com/.default"
_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _odata_str(value: str) -> str:
    """Quote a string literal for an OData $filter."""
    return "'" + value.replace("'", "''") + "'"


def _parse_date(value: str | None) -> date | None:
    if not value or value.startswith("0001-01-01"):
        return None
    return date.fromisoformat(value[:10])


class BusinessCentralERP(ERPClient):
    mode = "business_central"

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        environment: str,
        company_id: str | None = None,
        company_name: str | None = None,
        http: httpx.Client | None = None,
        max_retries: int = 3,
    ) -> None:
        missing = [n for n, v in (("BC_TENANT_ID", tenant_id), ("BC_CLIENT_ID", client_id), ("BC_CLIENT_SECRET", client_secret)) if not v]
        if missing:
            raise ERPError(f"Configuration Business Central incomplète : {', '.join(missing)}")
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = API_ROOT.format(tenant=tenant_id, environment=environment)
        self.http = http or httpx.Client(timeout=30.0)
        self.max_retries = max_retries
        self._token: str | None = None
        self._token_expiry = 0.0
        self._token_lock = threading.Lock()
        self._company_id = company_id or None
        self._company_name = company_name or None

    @classmethod
    def from_settings(cls, settings: Settings) -> "BusinessCentralERP":
        return cls(
            tenant_id=settings.bc_tenant_id,
            client_id=settings.bc_client_id,
            client_secret=settings.bc_client_secret,
            environment=settings.bc_environment,
            company_id=settings.bc_company_id,
            company_name=settings.bc_company_name,
        )

    # ------------------------------------------------------------------ auth
    def _get_token(self) -> str:
        with self._token_lock:
            if self._token and time.time() < self._token_expiry - 60:
                return self._token
            try:
                response = self.http.post(
                    TOKEN_URL.format(tenant=self.tenant_id),
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "scope": SCOPE,
                    },
                )
            except httpx.HTTPError as exc:
                raise ERPError(f"Entra ID injoignable : {exc}") from exc
            if response.status_code != 200:
                body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                raise ERPError(f"Authentification OAuth2 refusée : {body.get('error_description', response.text)[:300]}")
            payload = response.json()
            self._token = payload["access_token"]
            self._token_expiry = time.time() + int(payload.get("expires_in", 3600))
            return self._token

    # ------------------------------------------------------------------ http
    def _request(self, method: str, path: str, *, params: dict[str, str] | None = None,
                 json: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(self.max_retries + 1):
            request_headers = {"Authorization": f"Bearer {self._get_token()}", "Accept": "application/json"}
            request_headers.update(headers or {})
            try:
                response = self.http.request(method, url, params=params, json=json, headers=request_headers)
            except httpx.HTTPError as exc:
                if attempt < self.max_retries:
                    time.sleep(2**attempt)
                    continue
                raise ERPError(f"Business Central injoignable : {exc}") from exc

            if response.status_code == 401 and attempt == 0:
                self._token = None  # token revoked/expired: refresh once
                continue
            if response.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                delay = float(response.headers.get("Retry-After", 2**attempt))
                logger.warning("BC %s %s -> %s, retry in %.0fs", method, path, response.status_code, delay)
                time.sleep(min(delay, 30))
                continue
            if response.status_code >= 400:
                raise ERPError(self._error_message(response))
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        raise ERPError("Business Central : nombre maximal de tentatives atteint.")

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            error = response.json().get("error", {})
            return f"Business Central {response.status_code} ({error.get('code', '?')}) : {error.get('message', '')}"
        except ValueError:
            return f"Business Central {response.status_code} : {response.text[:300]}"

    def _get_all(self, path: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        payload = self._request("GET", path, params=params)
        rows.extend(payload.get("value", []))
        while next_link := payload.get("@odata.nextLink"):
            payload = self._request("GET", next_link)
            rows.extend(payload.get("value", []))
        return rows

    # ---------------------------------------------------------------- company
    @property
    def company_path(self) -> str:
        if not self._company_id:
            companies = self._get_all("companies")
            if not companies:
                raise ERPError("Aucune société accessible dans cet environnement Business Central.")
            chosen = next((c for c in companies if self._company_name and c.get("name") == self._company_name), companies[0])
            self._company_id = chosen["id"]
            logger.info("Business Central company: %s (%s)", chosen.get("name"), chosen["id"])
        return f"companies({self._company_id})"

    # ---------------------------------------------------------------- mappers
    @staticmethod
    def _vendor(row: dict[str, Any]) -> Vendor:
        blocked = (row.get("blocked") or "").strip()
        return Vendor(
            id=row["id"],
            number=row["number"],
            name=row.get("displayName", ""),
            vat_number=row.get("taxRegistrationNumber") or None,
            city=row.get("city") or None,
            currency=row.get("currencyCode") or "EUR",
            blocked=blocked not in ("", "_x0020_"),
        )

    @staticmethod
    def _order(row: dict[str, Any]) -> PurchaseOrder:
        lines = [
            PurchaseOrderLine(
                item_number=line.get("lineObjectNumber") or None,
                description=line.get("description", ""),
                quantity=float(line.get("quantity", 0)),
                unit_cost=float(line.get("directUnitCost", 0)),
                line_type=line.get("lineType", "Item"),
            )
            for line in row.get("purchaseOrderLines", [])
            if line.get("lineType") in ("Item", "Account", "Resource", "Fixed Asset", "Charge")
        ]
        return PurchaseOrder(
            id=row["id"],
            number=row["number"],
            vendor_number=row.get("vendorNumber", ""),
            vendor_name=row.get("vendorName", ""),
            order_date=_parse_date(row.get("orderDate")),
            status=row.get("status", ""),
            lines=lines,
            total_excl_tax=float(row.get("totalAmountExcludingTax", 0)),
            total_incl_tax=float(row.get("totalAmountIncludingTax", 0)),
        )

    @staticmethod
    def _invoice(row: dict[str, Any]) -> PurchaseInvoiceSummary:
        return PurchaseInvoiceSummary(
            id=row["id"],
            number=row.get("number", ""),
            vendor_number=row.get("vendorNumber", ""),
            vendor_invoice_number=row.get("vendorInvoiceNumber", ""),
            invoice_date=_parse_date(row.get("invoiceDate")),
            status=row.get("status", ""),
            total_excl_tax=row.get("totalAmountExcludingTax"),
            total_incl_tax=row.get("totalAmountIncludingTax"),
        )

    # ------------------------------------------------------------- operations
    def search_vendors(self, name: str | None = None, vat_number: str | None = None) -> list[tuple[Vendor, float]]:
        select = "id,number,displayName,taxRegistrationNumber,city,currencyCode,blocked"
        if vat_number:
            rows = self._get_all(
                f"{self.company_path}/vendors",
                {"$filter": f"taxRegistrationNumber eq {_odata_str(vat_number.replace(' ', ''))}", "$select": select},
            )
            if rows:
                return [(self._vendor(r), 1.0) for r in rows]
        # Fuzzy name matching is done client-side (OData has no similarity operator).
        rows = self._get_all(f"{self.company_path}/vendors", {"$select": select})
        return rank_vendors([self._vendor(r) for r in rows], name, vat_number)

    def list_open_purchase_orders(self, vendor_number: str) -> list[PurchaseOrder]:
        rows = self._get_all(
            f"{self.company_path}/purchaseOrders",
            {"$filter": f"vendorNumber eq {_odata_str(vendor_number)}", "$expand": "purchaseOrderLines"},
        )
        return [self._order(r) for r in rows]

    def get_purchase_order(self, number: str) -> PurchaseOrder | None:
        rows = self._get_all(
            f"{self.company_path}/purchaseOrders",
            {"$filter": f"number eq {_odata_str(number.strip())}", "$expand": "purchaseOrderLines"},
        )
        return self._order(rows[0]) if rows else None

    def find_purchase_invoices(self, vendor_number: str, vendor_invoice_number: str) -> list[PurchaseInvoiceSummary]:
        rows = self._get_all(
            f"{self.company_path}/purchaseInvoices",
            {
                "$filter": f"vendorNumber eq {_odata_str(vendor_number)} and "
                f"vendorInvoiceNumber eq {_odata_str(vendor_invoice_number.strip())}"
            },
        )
        return [self._invoice(r) for r in rows]

    def create_draft_purchase_invoice(self, request: DraftInvoiceRequest) -> PurchaseInvoiceSummary:
        header: dict[str, Any] = {
            "vendorNumber": request.vendor_number,
            "vendorInvoiceNumber": request.vendor_invoice_number,
            "invoiceDate": request.invoice_date.isoformat(),
        }
        if request.due_date:
            header["dueDate"] = request.due_date.isoformat()
        created = self._request("POST", f"{self.company_path}/purchaseInvoices", json=header)
        invoice_id = created["id"]
        try:
            for line in request.lines:
                body: dict[str, Any] = {
                    "lineType": line.line_type,
                    "description": line.description[:100],
                    "quantity": line.quantity,
                    "directUnitCost": line.unit_cost,
                }
                if line.object_number:
                    body["lineObjectNumber"] = line.object_number
                self._request("POST", f"{self.company_path}/purchaseInvoices({invoice_id})/purchaseInvoiceLines", json=body)
        except ERPError:
            # Roll back the header so no half-built invoice is left in BC.
            try:
                self.delete_draft_purchase_invoice(invoice_id)
            except ERPError:
                logger.exception("Rollback failed for purchase invoice %s", invoice_id)
            raise
        return self._invoice(self._request("GET", f"{self.company_path}/purchaseInvoices({invoice_id})"))

    def post_purchase_invoice(self, invoice_id: str) -> PurchaseInvoiceSummary:
        self._request("POST", f"{self.company_path}/purchaseInvoices({invoice_id})/Microsoft.NAV.post")
        return self._invoice(self._request("GET", f"{self.company_path}/purchaseInvoices({invoice_id})"))

    def delete_draft_purchase_invoice(self, invoice_id: str) -> None:
        self._request("DELETE", f"{self.company_path}/purchaseInvoices({invoice_id})", headers={"If-Match": "*"})
