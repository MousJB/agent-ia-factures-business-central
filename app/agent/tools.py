"""Tools exposed to the LLM (function calling) and their server-side execution.

Guardrails live here, not in the prompt: the draft invoice cannot be created
until the data was recorded, the vendor resolved and the duplicate check run.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import ValidationError

from app.agent import checks
from app.erp.base import ERPClient, ERPError
from app.llm.base import ToolSpec
from app.models import (
    Anomaly,
    DraftInvoiceRequest,
    DraftLine,
    ExtractedInvoice,
    InvoiceJob,
    PurchaseOrder,
    Severity,
)

# --------------------------------------------------------------------------- #
# Tool schemas
# --------------------------------------------------------------------------- #
_LINE_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "description": "Désignation telle qu'imprimée (avec la référence article si présente)"},
        "quantity": {"type": "number"},
        "unit_price": {"type": "number", "description": "Prix unitaire HT"},
        "line_total": {"type": ["number", "null"], "description": "Montant HT de la ligne tel qu'imprimé"},
    },
    "required": ["description", "quantity", "unit_price", "line_total"],
}

TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="record_invoice_data",
        description=(
            "Enregistre les données extraites de la facture PDF. À appeler EN PREMIER, une seule fois "
            "(ou à nouveau pour corriger une erreur d'extraction). Recopie les montants exactement tels "
            "qu'imprimés, même s'ils semblent faux : le contrôle arithmétique est fait par l'outil et son "
            "résultat est renvoyé."
        ),
        parameters={
            "type": "object",
            "properties": {
                "vendor_name": {"type": "string"},
                "vendor_vat_number": {"type": ["string", "null"], "description": "N° TVA intracommunautaire"},
                "invoice_number": {"type": "string"},
                "invoice_date": {"type": "string", "description": "Format YYYY-MM-DD"},
                "due_date": {"type": ["string", "null"], "description": "Format YYYY-MM-DD"},
                "purchase_order_reference": {"type": ["string", "null"], "description": "N° de commande cité"},
                "currency": {"type": "string", "description": "Code ISO, ex. EUR"},
                "lines": {"type": "array", "items": _LINE_SCHEMA},
                "subtotal_excl_tax": {"type": "number", "description": "Total HT imprimé"},
                "tax_amount": {"type": "number", "description": "Total TVA imprimé"},
                "total_incl_tax": {"type": "number", "description": "Total TTC imprimé"},
            },
            "required": [
                "vendor_name", "vendor_vat_number", "invoice_number", "invoice_date", "due_date",
                "purchase_order_reference", "currency", "lines", "subtotal_excl_tax", "tax_amount",
                "total_incl_tax",
            ],
        },
    ),
    ToolSpec(
        name="search_vendor",
        description="Recherche le fournisseur dans l'ERP par n° de TVA (prioritaire) et/ou par nom (recherche approchée).",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": ["string", "null"]},
                "vat_number": {"type": ["string", "null"]},
            },
            "required": ["name", "vat_number"],
        },
    ),
    ToolSpec(
        name="check_duplicate_invoice",
        description="Vérifie si ce n° de facture fournisseur existe déjà dans l'ERP pour ce fournisseur (brouillon ou validée).",
        parameters={
            "type": "object",
            "properties": {
                "vendor_number": {"type": "string", "description": "N° fournisseur ERP (ex. 10000)"},
                "invoice_number": {"type": "string", "description": "N° de facture du fournisseur"},
            },
            "required": ["vendor_number", "invoice_number"],
        },
    ),
    ToolSpec(
        name="find_purchase_order",
        description=(
            "Cherche le bon de commande correspondant. Si la facture cite un n° de commande, passe-le dans "
            "po_number ; sinon la liste des commandes ouvertes du fournisseur est renvoyée."
        ),
        parameters={
            "type": "object",
            "properties": {
                "vendor_number": {"type": "string"},
                "po_number": {"type": ["string", "null"]},
            },
            "required": ["vendor_number", "po_number"],
        },
    ),
    ToolSpec(
        name="compare_with_purchase_order",
        description="Rapproche la facture enregistrée avec un bon de commande (lignes, quantités, prix, total HT).",
        parameters={
            "type": "object",
            "properties": {"po_number": {"type": "string"}},
            "required": ["po_number"],
        },
    ),
    ToolSpec(
        name="create_draft_purchase_invoice",
        description=(
            "Crée la facture d'achat EN BROUILLON dans l'ERP à partir des données enregistrées. Refusé si le "
            "fournisseur est inconnu ou si la facture est un doublon. Ne valide/comptabilise jamais : la "
            "validation est faite par un humain."
        ),
        parameters={
            "type": "object",
            "properties": {
                "vendor_number": {"type": "string"},
                "purchase_order_number": {"type": ["string", "null"]},
            },
            "required": ["vendor_number", "purchase_order_number"],
        },
    ),
    ToolSpec(
        name="submit_final_report",
        description="Termine le traitement avec une synthèse pour le comptable. Toujours appeler en DERNIER.",
        parameters={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Synthèse en français, 2 à 5 phrases"},
                "recommendation": {
                    "type": "string",
                    "enum": ["validate", "review", "reject"],
                    "description": "validate = conforme ; review = à vérifier ; reject = à rejeter",
                },
            },
            "required": ["summary", "recommendation"],
        },
    ),
]


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
class InvoiceToolbox:
    """Executes tool calls against the ERP and accumulates state on the job."""

    def __init__(self, job: InvoiceJob, erp: ERPClient, default_gl_account: str = "607000") -> None:
        self.job = job
        self.erp = erp
        self.default_gl_account = default_gl_account
        self.duplicate_checked = False
        self.duplicate_found = False
        self.done = False
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "record_invoice_data": self._record_invoice_data,
            "search_vendor": self._search_vendor,
            "check_duplicate_invoice": self._check_duplicate_invoice,
            "find_purchase_order": self._find_purchase_order,
            "compare_with_purchase_order": self._compare_with_purchase_order,
            "create_draft_purchase_invoice": self._create_draft,
            "submit_final_report": self._submit_final_report,
        }

    # -------------------------------------------------------------- dispatcher
    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            return {"error": f"Outil inconnu : {name}"}
        try:
            return handler(arguments)
        except ValidationError as exc:
            return {"error": "Arguments invalides", "details": exc.errors(include_url=False, include_context=False)}
        except ERPError as exc:
            return {"error": f"Erreur ERP : {exc}"}

    def _add_anomalies(self, anomalies: list[Anomaly], replace_codes: set[str] | None = None) -> None:
        if replace_codes:
            self.job.anomalies = [a for a in self.job.anomalies if a.code not in replace_codes]
        existing = {(a.code, a.message) for a in self.job.anomalies}
        self.job.anomalies.extend(a for a in anomalies if (a.code, a.message) not in existing)

    # ------------------------------------------------------------------ tools
    def _record_invoice_data(self, args: dict[str, Any]) -> dict[str, Any]:
        invoice = ExtractedInvoice.model_validate(args)
        self.job.extracted = invoice
        report, anomalies = checks.check_invoice_totals(invoice)
        self._add_anomalies(
            anomalies,
            replace_codes={"TOTAL_INCONSISTENT", "LINE_TOTAL_INCONSISTENT", "UNUSUAL_VAT_RATE", "DUE_DATE_INVALID"},
        )
        return {"recorded": True, "arithmetic_check": report, "anomalies": [a.message for a in anomalies]}

    def _search_vendor(self, args: dict[str, Any]) -> dict[str, Any]:
        name, vat = args.get("name"), args.get("vat_number")
        if not name and not vat:
            return {"error": "Fournir au moins un nom ou un n° de TVA."}
        candidates = self.erp.search_vendors(name=name, vat_number=vat)
        if not candidates:
            self.job.vendor = None
            self._add_anomalies(
                [
                    Anomaly(
                        code="UNKNOWN_VENDOR",
                        severity=Severity.ERROR,
                        message=f"Fournisseur « {name or vat} » introuvable dans l'ERP"
                        + (f" (TVA {vat})" if vat else "")
                        + ". Création de la fiche fournisseur nécessaire avant saisie.",
                    )
                ]
            )
            return {"found": False, "candidates": []}

        best, score = candidates[0]
        self.job.vendor = best
        self.job.anomalies = [a for a in self.job.anomalies if a.code != "UNKNOWN_VENDOR"]
        if best.blocked:
            self._add_anomalies(
                [Anomaly(code="VENDOR_BLOCKED", severity=Severity.ERROR, message=f"Le fournisseur {best.number} est bloqué.")]
            )
        if score < 0.85:
            self._add_anomalies(
                [
                    Anomaly(
                        code="VENDOR_FUZZY_MATCH",
                        severity=Severity.WARNING,
                        message=f"Fournisseur rapproché par similarité de nom ({score:.0%}) : « {best.name} ».",
                    )
                ]
            )
        return {
            "found": True,
            "best_match": best.model_dump(),
            "match_score": score,
            "candidates": [{"number": v.number, "name": v.name, "score": s} for v, s in candidates[:5]],
        }

    def _check_duplicate_invoice(self, args: dict[str, Any]) -> dict[str, Any]:
        vendor_number = str(args["vendor_number"])
        invoice_number = str(args["invoice_number"])
        existing = self.erp.find_purchase_invoices(vendor_number, invoice_number)
        self.duplicate_checked = True
        self.duplicate_found = bool(existing)
        if existing:
            first = existing[0]
            self._add_anomalies(
                [
                    Anomaly(
                        code="DUPLICATE_INVOICE",
                        severity=Severity.ERROR,
                        message=f"La facture {invoice_number} existe déjà dans l'ERP (pièce {first.number}, "
                        f"statut {first.status}, du {first.invoice_date}).",
                        details={"existing": [e.model_dump(mode="json") for e in existing]},
                    )
                ]
            )
        return {"duplicate": bool(existing), "existing_invoices": [e.model_dump(mode="json") for e in existing]}

    def _find_purchase_order(self, args: dict[str, Any]) -> dict[str, Any]:
        vendor_number = str(args["vendor_number"])
        po_number = (args.get("po_number") or "").strip()
        if po_number:
            po = self.erp.get_purchase_order(po_number)
            if po and po.vendor_number == vendor_number:
                return {"found": True, "purchase_orders": [self._po_view(po)]}
            note = (
                f"La commande {po_number} appartient à un autre fournisseur ({po.vendor_name})."
                if po
                else f"Commande {po_number} introuvable."
            )
        else:
            note = None
        orders = self.erp.list_open_purchase_orders(vendor_number)
        if not orders:
            self._add_anomalies(
                [
                    Anomaly(
                        code="NO_PURCHASE_ORDER",
                        severity=Severity.WARNING,
                        message="Aucun bon de commande ouvert pour ce fournisseur : facture hors commande.",
                    )
                ]
            )
        return {"found": bool(orders), "note": note, "purchase_orders": [self._po_view(po) for po in orders]}

    @staticmethod
    def _po_view(po: PurchaseOrder) -> dict[str, Any]:
        return po.model_dump(mode="json", exclude={"id"})

    def _compare_with_purchase_order(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.job.extracted is None:
            return {"error": "Appeler record_invoice_data d'abord."}
        po = self.erp.get_purchase_order(str(args["po_number"]))
        if po is None:
            return {"error": f"Commande {args['po_number']} introuvable."}
        self.job.purchase_order = po
        report, anomalies = checks.compare_with_purchase_order(self.job.extracted, po)
        self._add_anomalies(
            anomalies,
            replace_codes={"AMOUNT_MISMATCH_PO", "PRICE_MISMATCH_PO", "QUANTITY_MISMATCH_PO", "LINE_NOT_ON_PO", "NO_PURCHASE_ORDER"},
        )
        return {"comparison": report, "anomalies": [a.message for a in anomalies]}

    def _create_draft(self, args: dict[str, Any]) -> dict[str, Any]:
        invoice = self.job.extracted
        if invoice is None:
            return {"error": "Refusé : aucune donnée de facture enregistrée (record_invoice_data)."}
        if self.job.vendor is None or self.job.vendor.number != str(args["vendor_number"]):
            return {"error": "Refusé : fournisseur non identifié dans l'ERP (search_vendor). Aucun brouillon créé."}
        if self.job.vendor.blocked:
            return {"error": "Refusé : fournisseur bloqué."}
        if not self.duplicate_checked:
            return {"error": "Refusé : exécuter check_duplicate_invoice avant de créer le brouillon."}
        if self.duplicate_found:
            return {"error": "Refusé : facture en doublon, aucun brouillon créé."}
        if self.job.draft_invoice is not None:
            return {"error": f"Un brouillon existe déjà : {self.job.draft_invoice.number}."}

        po: PurchaseOrder | None = None
        po_number = args.get("purchase_order_number")
        if po_number:
            po = self.erp.get_purchase_order(str(po_number))
        lines: list[DraftLine] = []
        for line in invoice.lines:
            po_line = None
            if po:
                idx, _ = checks.match_po_line(line.description, po.lines)
                po_line = po.lines[idx] if idx is not None else None
            lines.append(
                DraftLine(
                    line_type=po_line.line_type if po_line else "Account",
                    object_number=po_line.item_number if po_line else self.default_gl_account,
                    description=line.description[:100],
                    quantity=line.quantity,
                    unit_cost=line.unit_price,  # what is billed; gaps are flagged as anomalies
                )
            )
        draft = self.erp.create_draft_purchase_invoice(
            DraftInvoiceRequest(
                vendor_number=self.job.vendor.number,
                vendor_invoice_number=invoice.invoice_number,
                invoice_date=invoice.invoice_date,
                due_date=invoice.due_date,
                purchase_order_number=po.number if po else None,
                lines=lines,
            )
        )
        self.job.draft_invoice = draft
        return {"created": True, "draft_invoice": draft.model_dump(mode="json"), "status": "Draft"}

    def _submit_final_report(self, args: dict[str, Any]) -> dict[str, Any]:
        recommendation = args.get("recommendation")
        if recommendation not in {"validate", "review", "reject"}:
            return {"error": "recommendation doit valoir validate, review ou reject."}
        self.job.summary = str(args.get("summary", "")).strip()
        self.job.recommendation = recommendation
        self.done = True
        return {"ok": True}
