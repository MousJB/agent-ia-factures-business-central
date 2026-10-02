"""Abstract ERP interface. The agent only talks to this contract."""

from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod
from difflib import SequenceMatcher

from app.models import (
    DraftInvoiceRequest,
    PurchaseInvoiceSummary,
    PurchaseOrder,
    Vendor,
)


class ERPError(Exception):
    """Raised when the ERP backend fails or rejects an operation."""


class ERPClient(ABC):
    """Operations the invoice agent needs from an ERP."""

    mode: str = "abstract"

    @abstractmethod
    def search_vendors(self, name: str | None = None, vat_number: str | None = None) -> list[tuple[Vendor, float]]:
        """Return candidate vendors with a match score in [0, 1], best first."""

    @abstractmethod
    def list_open_purchase_orders(self, vendor_number: str) -> list[PurchaseOrder]:
        """Return purchase orders of a vendor that can still be invoiced."""

    @abstractmethod
    def get_purchase_order(self, number: str) -> PurchaseOrder | None:
        """Return a purchase order by its number, or None."""

    @abstractmethod
    def find_purchase_invoices(self, vendor_number: str, vendor_invoice_number: str) -> list[PurchaseInvoiceSummary]:
        """Return existing (draft or posted) invoices with this vendor invoice number."""

    @abstractmethod
    def create_draft_purchase_invoice(self, request: DraftInvoiceRequest) -> PurchaseInvoiceSummary:
        """Create a purchase invoice in Draft status."""

    @abstractmethod
    def post_purchase_invoice(self, invoice_id: str) -> PurchaseInvoiceSummary:
        """Post (validate) a draft invoice after human approval."""

    @abstractmethod
    def delete_draft_purchase_invoice(self, invoice_id: str) -> None:
        """Delete a draft invoice after human rejection."""


# --------------------------------------------------------------------------- #
# Fuzzy matching helpers shared by all implementations
# --------------------------------------------------------------------------- #
_LEGAL_FORMS = {
    "inc", "sa", "sas", "sarl", "eurl", "ltd", "llc", "gmbh", "corp", "co", "company",
    "société", "societe", "the", "et", "and", "group", "groupe",
}


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-z0-9 ]", " ", value.lower())
    tokens = [t for t in value.split() if t not in _LEGAL_FORMS]
    return " ".join(tokens)


def normalize_vat(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def name_similarity(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    tokens_a, tokens_b = set(na.split()), set(nb.split())
    overlap = len(tokens_a & tokens_b) / max(len(tokens_a | tokens_b), 1)
    return round(max(ratio, overlap), 3)


def rank_vendors(
    vendors: list[Vendor], name: str | None, vat_number: str | None, threshold: float = 0.6
) -> list[tuple[Vendor, float]]:
    vat = normalize_vat(vat_number)
    scored: list[tuple[Vendor, float]] = []
    for vendor in vendors:
        if vat and normalize_vat(vendor.vat_number) == vat:
            scored.append((vendor, 1.0))
            continue
        score = name_similarity(name, vendor.name) if name else 0.0
        if score >= threshold:
            scored.append((vendor, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored
