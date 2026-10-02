"""Deterministic business controls.

The LLM orchestrates, but every anomaly is computed here by plain code so that
it can neither be hallucinated nor silently dropped by the model.
"""

from __future__ import annotations

from typing import Any

from app.erp.base import name_similarity
from app.models import Anomaly, ExtractedInvoice, PurchaseOrder, PurchaseOrderLine, Severity

MONEY_EPSILON = 0.02  # rounding tolerance on printed amounts (€)
PO_TOLERANCE_ABS = 1.00  # accepted gap vs purchase order (€)
PO_TOLERANCE_PCT = 0.005  # ... or 0.5 %
STANDARD_VAT_RATES = (0.20, 0.10, 0.055, 0.021, 0.0)


def eur(value: float, signed: bool = False) -> str:
    """French money format: 1 234,56 €."""
    text = f"{value:+,.2f}" if signed else f"{value:,.2f}"
    return text.replace(",", " ").replace(".", ",") + " €"


def pct(value: float, signed: bool = True, digits: int = 1) -> str:
    """French percent format: +12,0 %."""
    text = f"{value * 100:+.{digits}f}" if signed else f"{value * 100:.{digits}f}"
    return text.replace(".", ",") + " %"


def _r(value: float) -> float:
    return round(value + 0.0, 2)


def check_invoice_totals(invoice: ExtractedInvoice) -> tuple[dict[str, Any], list[Anomaly]]:
    """Verify the internal arithmetic of the invoice."""
    anomalies: list[Anomaly] = []
    computed_lines = [_r(line.quantity * line.unit_price) for line in invoice.lines]
    lines_sum = _r(sum(computed_lines))
    expected_ttc = _r(invoice.subtotal_excl_tax + invoice.tax_amount)
    vat_rate = invoice.tax_amount / invoice.subtotal_excl_tax if invoice.subtotal_excl_tax else 0.0

    for idx, (line, computed) in enumerate(zip(invoice.lines, computed_lines), start=1):
        if line.line_total is not None and abs(line.line_total - computed) > MONEY_EPSILON:
            anomalies.append(
                Anomaly(
                    code="LINE_TOTAL_INCONSISTENT",
                    severity=Severity.ERROR,
                    message=f"Ligne {idx} « {line.description} » : {line.quantity:g} × {eur(line.unit_price)} = "
                    f"{eur(computed)}, mais {eur(line.line_total)} imprimé.",
                    details={"line": idx, "computed": computed, "printed": line.line_total},
                )
            )

    if abs(lines_sum - invoice.subtotal_excl_tax) > MONEY_EPSILON:
        anomalies.append(
            Anomaly(
                code="TOTAL_INCONSISTENT",
                severity=Severity.ERROR,
                message=f"Somme des lignes {eur(lines_sum)} ≠ total HT imprimé {eur(invoice.subtotal_excl_tax)}.",
                details={"lines_sum": lines_sum, "printed_subtotal": invoice.subtotal_excl_tax},
            )
        )
    if abs(expected_ttc - invoice.total_incl_tax) > MONEY_EPSILON:
        anomalies.append(
            Anomaly(
                code="TOTAL_INCONSISTENT",
                severity=Severity.ERROR,
                message=f"HT {eur(invoice.subtotal_excl_tax)} + TVA {eur(invoice.tax_amount)} = {eur(expected_ttc)}, "
                f"mais total TTC imprimé {eur(invoice.total_incl_tax)} "
                f"(écart {eur(invoice.total_incl_tax - expected_ttc, signed=True)}).",
                details={"expected_ttc": expected_ttc, "printed_ttc": invoice.total_incl_tax},
            )
        )
    if invoice.subtotal_excl_tax and not any(abs(vat_rate - rate) < 0.002 for rate in STANDARD_VAT_RATES):
        anomalies.append(
            Anomaly(
                code="UNUSUAL_VAT_RATE",
                severity=Severity.WARNING,
                message=f"Taux de TVA apparent {pct(vat_rate, signed=False, digits=2)} non standard en France.",
                details={"vat_rate": round(vat_rate, 4)},
            )
        )
    if invoice.due_date and invoice.due_date < invoice.invoice_date:
        anomalies.append(
            Anomaly(
                code="DUE_DATE_INVALID",
                severity=Severity.WARNING,
                message="La date d'échéance est antérieure à la date de facture.",
            )
        )

    report = {
        "computed_line_totals": computed_lines,
        "lines_sum": lines_sum,
        "printed_subtotal_excl_tax": invoice.subtotal_excl_tax,
        "expected_total_incl_tax": expected_ttc,
        "printed_total_incl_tax": invoice.total_incl_tax,
        "apparent_vat_rate": round(vat_rate, 4),
        "consistent": not any(a.severity == Severity.ERROR for a in anomalies),
    }
    return report, anomalies


def match_po_line(description: str, po_lines: list[PurchaseOrderLine]) -> tuple[int | None, float]:
    """Return the index of the PO line that best matches an invoice description."""
    best_idx, best_score = None, 0.0
    upper = description.upper()
    for idx, po_line in enumerate(po_lines):
        if po_line.item_number and po_line.line_type == "Item" and po_line.item_number.upper() in upper:
            return idx, 1.0
        score = name_similarity(description, po_line.description)
        if score > best_score:
            best_idx, best_score = idx, score
    return (best_idx, best_score) if best_score >= 0.5 else (None, best_score)


def compare_with_purchase_order(
    invoice: ExtractedInvoice, po: PurchaseOrder
) -> tuple[dict[str, Any], list[Anomaly]]:
    """Three-way-match light: invoice vs purchase order, line by line and in total."""
    anomalies: list[Anomaly] = []
    line_reports: list[dict[str, Any]] = []
    used: set[int] = set()

    for idx, line in enumerate(invoice.lines, start=1):
        po_idx, score = match_po_line(line.description, po.lines)
        if po_idx is None or po_idx in used:
            line_reports.append({"invoice_line": idx, "description": line.description, "matched": False})
            anomalies.append(
                Anomaly(
                    code="LINE_NOT_ON_PO",
                    severity=Severity.WARNING,
                    message=f"Ligne {idx} « {line.description} » absente du bon de commande {po.number}.",
                )
            )
            continue
        used.add(po_idx)
        po_line = po.lines[po_idx]
        qty_gap = _r(line.quantity - po_line.quantity)
        price_gap = _r(line.unit_price - po_line.unit_cost)
        line_reports.append(
            {
                "invoice_line": idx,
                "description": line.description,
                "matched_po_line": po_line.description,
                "match_score": round(score, 2),
                "invoice_qty": line.quantity,
                "po_qty": po_line.quantity,
                "invoice_unit_price": line.unit_price,
                "po_unit_cost": po_line.unit_cost,
                "qty_gap": qty_gap,
                "unit_price_gap": price_gap,
            }
        )
        if abs(qty_gap) > 1e-9:
            anomalies.append(
                Anomaly(
                    code="QUANTITY_MISMATCH_PO",
                    severity=Severity.WARNING,
                    message=f"« {po_line.description} » : {line.quantity:g} facturé(s) pour {po_line.quantity:g} "
                    f"commandé(s).",
                    details={"invoice_qty": line.quantity, "po_qty": po_line.quantity},
                )
            )
        if abs(price_gap) > MONEY_EPSILON:
            pct_value = price_gap / po_line.unit_cost if po_line.unit_cost else 0.0
            anomalies.append(
                Anomaly(
                    code="PRICE_MISMATCH_PO",
                    severity=Severity.WARNING,
                    message=f"« {po_line.description} » : PU facturé {eur(line.unit_price)} vs "
                    f"{eur(po_line.unit_cost)} commandé ({pct(pct_value)}).",
                    details={"invoice_unit_price": line.unit_price, "po_unit_cost": po_line.unit_cost},
                )
            )

    gap = _r(invoice.subtotal_excl_tax - po.total_excl_tax)
    tolerance = max(PO_TOLERANCE_ABS, po.total_excl_tax * PO_TOLERANCE_PCT)
    if abs(gap) > tolerance:
        pct_value = gap / po.total_excl_tax if po.total_excl_tax else 0.0
        anomalies.append(
            Anomaly(
                code="AMOUNT_MISMATCH_PO",
                severity=Severity.ERROR,
                message=f"Total HT facturé {eur(invoice.subtotal_excl_tax)} vs {eur(po.total_excl_tax)} sur la "
                f"commande {po.number} : écart {eur(gap, signed=True)} ({pct(pct_value)}), au-delà de la tolérance "
                f"de {eur(tolerance)}.",
                details={"gap": gap, "gap_pct": round(pct_value, 4), "tolerance": round(tolerance, 2)},
            )
        )

    report = {
        "purchase_order": po.number,
        "po_total_excl_tax": po.total_excl_tax,
        "invoice_total_excl_tax": invoice.subtotal_excl_tax,
        "gap_excl_tax": gap,
        "tolerance": round(tolerance, 2),
        "lines": line_reports,
        "within_tolerance": abs(gap) <= tolerance,
    }
    return report, anomalies
