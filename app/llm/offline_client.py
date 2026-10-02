"""Offline, deterministic stand-in for an LLM.

Lets the demo run end-to-end with no API key: extraction is done by regular
expressions tuned for the generated sample invoices, and the tool sequence
follows the same procedure the real LLM is prompted with. It exercises exactly
the same tools, guardrails and UI as the real providers.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.agent.checks import eur
from app.llm.base import AgentCallbacks, LLMClient, LLMError, ToolSpec

_MONEY = re.compile(r"^-?[\d  ]+,\d{2}\s*€?$")
_QTY = re.compile(r"^\d+(?:,\d+)?$")


def _money(value: str) -> float:
    return float(value.replace("€", "").replace(" ", "").replace(" ", "").replace(",", ".").strip())


def _date(value: str | None) -> str | None:
    if not value:
        return None
    return datetime.strptime(value, "%d/%m/%Y").date().isoformat()


def _search(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def parse_invoice_text(text: str) -> dict[str, Any]:
    """Best-effort rule-based extraction (sample invoice layout)."""
    rows = [row.strip() for row in text.splitlines() if row.strip()]
    if not rows:
        raise LLMError("Texte de facture vide.")

    lines: list[dict[str, Any]] = []
    try:
        start = rows.index("Montant HT") + 1
        end = next(i for i in range(start, len(rows)) if rows[i].startswith("Total HT"))
    except (ValueError, StopIteration) as exc:
        raise LLMError("Mode offline : tableau des lignes non reconnu. Utiliser un vrai LLM pour ce document.") from exc
    buffer: list[str] = []
    for row in rows[start:end]:
        buffer.append(row)
        if len(buffer) >= 4 and _QTY.match(buffer[-3]) and _MONEY.match(buffer[-2]) and _MONEY.match(buffer[-1]):
            description = " ".join(buffer[:-3])
            lines.append(
                {
                    "description": description,
                    "quantity": float(buffer[-3].replace(",", ".")),
                    "unit_price": _money(buffer[-2]),
                    "line_total": _money(buffer[-1]),
                }
            )
            buffer = []

    def amount_after(label: str) -> float:
        idx = next((i for i, r in enumerate(rows) if r.startswith(label)), None)
        if idx is None or idx + 1 >= len(rows):
            raise LLMError(f"Mode offline : montant « {label} » introuvable.")
        return _money(rows[idx + 1])

    invoice_number = _search(r"Facture N°\s*:\s*(\S+)", text)
    invoice_date = _date(_search(r"^Date\s*:\s*(\d{2}/\d{2}/\d{4})", text))
    if not invoice_number or not invoice_date:
        raise LLMError("Mode offline : n° ou date de facture introuvable.")
    return {
        "vendor_name": rows[0],
        "vendor_vat_number": _search(r"TVA\s*:\s*([A-Z]{2}[0-9A-Z]+)", text),
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "due_date": _date(_search(r"[ÉE]ch[ée]ance\s*:\s*(\d{2}/\d{2}/\d{4})", text)),
        "purchase_order_reference": _search(r"commande\s*:\s*(\S+)", text),
        "currency": "EUR",
        "lines": lines,
        "subtotal_excl_tax": amount_after("Total HT"),
        "tax_amount": amount_after("TVA "),
        "total_incl_tax": amount_after("Total TTC"),
    }


class OfflineClient(LLMClient):
    provider = "offline"
    supports_native_pdf = False

    def run_agent(
        self,
        *,
        system: str,
        user_text: str,
        pdf_bytes: bytes | None,
        tools: list[ToolSpec],
        callbacks: AgentCallbacks,
        max_iterations: int,
    ) -> str:
        match = re.search(r"<invoice_text>\n(.*)\n</invoice_text>", user_text, re.DOTALL)
        data = parse_invoice_text(match.group(1) if match else user_text)
        think = callbacks.on_thought
        call = callbacks.execute_tool

        think(f"Je lis la facture {data['invoice_number']} de « {data['vendor_name']} » et j'enregistre les données extraites.")
        recorded = call("record_invoice_data", data)
        if "error" in recorded:
            raise LLMError(f"Extraction rejetée : {recorded['error']}")
        consistent = recorded["arithmetic_check"]["consistent"]
        anomalies: list[str] = list(recorded["anomalies"])

        think("Je cherche le fournisseur dans l'ERP, d'abord par n° de TVA puis par nom.")
        vendor = call("search_vendor", {"name": data["vendor_name"], "vat_number": data["vendor_vat_number"]})
        if not vendor.get("found"):
            call(
                "submit_final_report",
                {
                    "summary": f"Fournisseur « {data['vendor_name']} » (TVA {data['vendor_vat_number']}) inconnu de "
                    f"l'ERP : aucun brouillon créé. Facture de {eur(data['total_incl_tax'])} TTC à transmettre "
                    "aux achats pour création de la fiche fournisseur, ou à rejeter si non sollicitée.",
                    "recommendation": "reject",
                },
            )
            return ""
        vendor_no = vendor["best_match"]["number"]

        think(f"Fournisseur {vendor_no} identifié. Je vérifie que ce n° de facture n'a pas déjà été saisi.")
        dup = call("check_duplicate_invoice", {"vendor_number": vendor_no, "invoice_number": data["invoice_number"]})
        if dup.get("duplicate"):
            existing = dup["existing_invoices"][0]
            call(
                "submit_final_report",
                {
                    "summary": f"Doublon : la facture {data['invoice_number']} est déjà enregistrée (pièce "
                    f"{existing['number']}, statut {existing['status']}). Aucun brouillon créé pour éviter un "
                    "double paiement.",
                    "recommendation": "reject",
                },
            )
            return ""

        think("Pas de doublon. Je recherche le bon de commande correspondant.")
        found = call("find_purchase_order", {"vendor_number": vendor_no, "po_number": data["purchase_order_reference"]})
        orders = found.get("purchase_orders", [])
        po_number = None
        if orders:
            po_number = orders[0]["number"]
            think(f"Je rapproche la facture de la commande {po_number}.")
            comparison = call("compare_with_purchase_order", {"po_number": po_number})
            anomalies += comparison.get("anomalies", [])

        think("Je crée la facture d'achat en brouillon ; la validation reste au comptable.")
        draft = call("create_draft_purchase_invoice", {"vendor_number": vendor_no, "purchase_order_number": po_number})
        draft_no = draft.get("draft_invoice", {}).get("number")

        if not anomalies and consistent:
            summary = (
                f"Facture {data['invoice_number']} conforme à la commande {po_number} : "
                f"{eur(data['subtotal_excl_tax'])} HT / {eur(data['total_incl_tax'])} TTC. "
                f"Brouillon {draft_no} prêt à être validé."
            )
            recommendation = "validate"
        else:
            summary = (
                f"Brouillon {draft_no} créé pour {eur(data['total_incl_tax'])} TTC, mais "
                f"{len(anomalies)} point(s) à vérifier : " + " ".join(anomalies)
            )
            recommendation = "review"
        call("submit_final_report", {"summary": summary, "recommendation": recommendation})
        return ""
