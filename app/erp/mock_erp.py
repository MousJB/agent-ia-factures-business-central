"""Local JSON-backed ERP inspired by the Business Central "CRONUS" demo company.

Seed data lives in ``data/seed``; a working copy is kept in ``data/runtime`` so
that drafts/postings survive restarts and the demo can be reset at any time.
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import Any

from app.erp.base import ERPClient, ERPError, rank_vendors
from app.models import (
    DraftInvoiceRequest,
    PurchaseInvoiceSummary,
    PurchaseOrder,
    Vendor,
)

_FILES = ("vendors.json", "purchase_orders.json", "purchase_invoices.json")


class MockERP(ERPClient):
    mode = "mock"

    def __init__(self, data_dir: Path) -> None:
        self._seed_dir = data_dir / "seed"
        self._runtime_dir = data_dir / "runtime"
        self._lock = threading.RLock()
        self._ensure_runtime()

    # ------------------------------------------------------------------ storage
    def _ensure_runtime(self) -> None:
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        for name in _FILES:
            target = self._runtime_dir / name
            if not target.exists():
                shutil.copyfile(self._seed_dir / name, target)

    def reset(self) -> None:
        with self._lock:
            for name in _FILES:
                shutil.copyfile(self._seed_dir / name, self._runtime_dir / name)

    def _load(self, name: str) -> list[dict[str, Any]]:
        try:
            return json.loads((self._runtime_dir / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ERPError(f"Données mock illisibles ({name}) : {exc}") from exc

    def _save(self, name: str, rows: list[dict[str, Any]]) -> None:
        (self._runtime_dir / name).write_text(
            json.dumps(rows, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )

    # ------------------------------------------------------------------ vendors
    def search_vendors(self, name: str | None = None, vat_number: str | None = None) -> list[tuple[Vendor, float]]:
        with self._lock:
            vendors = [Vendor.model_validate(row) for row in self._load("vendors.json")]
        return rank_vendors(vendors, name, vat_number)

    # ---------------------------------------------------------- purchase orders
    def _orders(self) -> list[PurchaseOrder]:
        return [PurchaseOrder.model_validate(row) for row in self._load("purchase_orders.json")]

    def list_open_purchase_orders(self, vendor_number: str) -> list[PurchaseOrder]:
        with self._lock:
            return [po for po in self._orders() if po.vendor_number == vendor_number and po.status != "Closed"]

    def get_purchase_order(self, number: str) -> PurchaseOrder | None:
        with self._lock:
            return next((po for po in self._orders() if po.number == number.strip()), None)

    # -------------------------------------------------------- purchase invoices
    def find_purchase_invoices(self, vendor_number: str, vendor_invoice_number: str) -> list[PurchaseInvoiceSummary]:
        wanted = vendor_invoice_number.strip().upper()
        with self._lock:
            rows = self._load("purchase_invoices.json")
        return [
            PurchaseInvoiceSummary.model_validate(row)
            for row in rows
            if row["vendor_number"] == vendor_number and row["vendor_invoice_number"].strip().upper() == wanted
        ]

    def create_draft_purchase_invoice(self, request: DraftInvoiceRequest) -> PurchaseInvoiceSummary:
        if not request.lines:
            raise ERPError("Impossible de créer une facture sans ligne.")
        with self._lock:
            rows = self._load("purchase_invoices.json")
            next_no = max((int(r["number"]) for r in rows if str(r["number"]).isdigit()), default=108000) + 1
            total = round(sum(line.quantity * line.unit_cost for line in request.lines), 2)
            row: dict[str, Any] = {
                "id": f"pi-{next_no}",
                "number": str(next_no),
                "vendor_number": request.vendor_number,
                "vendor_invoice_number": request.vendor_invoice_number,
                "invoice_date": request.invoice_date.isoformat(),
                "due_date": request.due_date.isoformat() if request.due_date else None,
                "purchase_order_number": request.purchase_order_number,
                "status": "Draft",
                "total_excl_tax": total,
                "lines": [line.model_dump() for line in request.lines],
            }
            rows.append(row)
            self._save("purchase_invoices.json", rows)
        return PurchaseInvoiceSummary.model_validate(row)

    def _update_status(self, invoice_id: str, status: str) -> PurchaseInvoiceSummary:
        with self._lock:
            rows = self._load("purchase_invoices.json")
            row = next((r for r in rows if r["id"] == invoice_id), None)
            if row is None:
                raise ERPError(f"Facture {invoice_id} introuvable.")
            if row["status"] != "Draft":
                raise ERPError(f"La facture {row['number']} n'est pas en brouillon (statut : {row['status']}).")
            row["status"] = status
            self._save("purchase_invoices.json", rows)
        return PurchaseInvoiceSummary.model_validate(row)

    def post_purchase_invoice(self, invoice_id: str) -> PurchaseInvoiceSummary:
        return self._update_status(invoice_id, "Open")

    def delete_draft_purchase_invoice(self, invoice_id: str) -> None:
        with self._lock:
            rows = self._load("purchase_invoices.json")
            row = next((r for r in rows if r["id"] == invoice_id), None)
            if row is None:
                raise ERPError(f"Facture {invoice_id} introuvable.")
            if row["status"] != "Draft":
                raise ERPError("Seules les factures en brouillon peuvent être supprimées.")
            rows.remove(row)
            self._save("purchase_invoices.json", rows)
