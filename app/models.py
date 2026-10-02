"""Domain models shared by the agent, the ERP layer and the API."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Extracted invoice
# --------------------------------------------------------------------------- #
class InvoiceLine(BaseModel):
    description: str = Field(description="Désignation de la ligne")
    quantity: float = Field(description="Quantité")
    unit_price: float = Field(description="Prix unitaire HT")
    line_total: float | None = Field(default=None, description="Montant HT de la ligne, tel qu'imprimé")


class ExtractedInvoice(BaseModel):
    vendor_name: str
    vendor_vat_number: str | None = None
    invoice_number: str
    invoice_date: date
    due_date: date | None = None
    purchase_order_reference: str | None = Field(
        default=None, description="Référence de bon de commande citée sur la facture, si présente"
    )
    currency: str = "EUR"
    lines: list[InvoiceLine]
    subtotal_excl_tax: float = Field(description="Total HT")
    tax_amount: float = Field(description="Montant de TVA")
    total_incl_tax: float = Field(description="Total TTC")


# --------------------------------------------------------------------------- #
# ERP entities (provider-neutral)
# --------------------------------------------------------------------------- #
class Vendor(BaseModel):
    id: str
    number: str
    name: str
    vat_number: str | None = None
    city: str | None = None
    currency: str = "EUR"
    blocked: bool = False


class PurchaseOrderLine(BaseModel):
    item_number: str | None = None
    description: str
    quantity: float
    unit_cost: float
    line_type: str = "Item"


class PurchaseOrder(BaseModel):
    id: str
    number: str
    vendor_number: str
    vendor_name: str
    order_date: date | None = None
    status: str = "Released"
    lines: list[PurchaseOrderLine] = []
    total_excl_tax: float
    total_incl_tax: float | None = None


class PurchaseInvoiceSummary(BaseModel):
    id: str
    number: str
    vendor_number: str
    vendor_invoice_number: str
    invoice_date: date | None = None
    status: str
    total_excl_tax: float | None = None
    total_incl_tax: float | None = None


class DraftLine(BaseModel):
    line_type: str = "Item"  # "Item" | "Account"
    object_number: str | None = None  # item no. or G/L account no.
    description: str
    quantity: float
    unit_cost: float


class DraftInvoiceRequest(BaseModel):
    vendor_number: str
    vendor_invoice_number: str
    invoice_date: date
    due_date: date | None = None
    purchase_order_number: str | None = None
    lines: list[DraftLine]


# --------------------------------------------------------------------------- #
# Agent trace & result
# --------------------------------------------------------------------------- #
class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Anomaly(BaseModel):
    code: str
    severity: Severity
    message: str
    details: dict[str, Any] = {}


class StepKind(str, Enum):
    THOUGHT = "thought"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ERROR = "error"
    FINAL = "final"
    HUMAN = "human"


class AgentStep(BaseModel):
    index: int
    kind: StepKind
    title: str
    content: str = ""
    tool_name: str | None = None
    payload: dict[str, Any] | None = None
    timestamp: datetime = Field(default_factory=datetime.now)


class JobStatus(str, Enum):
    PROCESSING = "processing"
    AWAITING_VALIDATION = "awaiting_validation"  # draft created, waiting for a human
    BLOCKED = "blocked"  # agent could not create a draft (unknown vendor, duplicate...)
    VALIDATED = "validated"
    REJECTED = "rejected"
    FAILED = "failed"


class InvoiceJob(BaseModel):
    id: str
    filename: str
    status: JobStatus = JobStatus.PROCESSING
    created_at: datetime = Field(default_factory=datetime.now)
    llm_provider: str
    erp_mode: str
    extracted: ExtractedInvoice | None = None
    vendor: Vendor | None = None
    purchase_order: PurchaseOrder | None = None
    draft_invoice: PurchaseInvoiceSummary | None = None
    anomalies: list[Anomaly] = []
    steps: list[AgentStep] = []
    summary: str | None = None
    recommendation: str | None = None  # "validate" | "review" | "reject"
    error: str | None = None
    human_comment: str | None = None
    duration_seconds: float | None = None
