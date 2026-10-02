"""Generate the demo supplier invoices (PDF) in ./samples.

Usage:  python scripts/generate_invoices.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT_DIR = Path(__file__).resolve().parent.parent / "samples"


@dataclass
class Line:
    ref: str
    description: str
    qty: float
    unit_price: float

    @property
    def total(self) -> float:
        return round(self.qty * self.unit_price, 2)


@dataclass
class Invoice:
    filename: str
    vendor: str
    address: list[str]
    vat: str
    siret: str
    color: str
    number: str
    issued: date
    due: date
    po_ref: str | None
    lines: list[Line]
    vat_rate: float = 0.20
    printed_ttc_override: float | None = None  # to simulate an inconsistent total
    notes: list[str] = field(default_factory=list)

    @property
    def subtotal(self) -> float:
        return round(sum(line.total for line in self.lines), 2)

    @property
    def tax(self) -> float:
        return round(self.subtotal * self.vat_rate, 2)

    @property
    def ttc(self) -> float:
        return self.printed_ttc_override if self.printed_ttc_override is not None else round(self.subtotal + self.tax, 2)


def eur(value: float) -> str:
    whole, dec = f"{value:,.2f}".split(".")
    return f"{whole.replace(',', ' ')},{dec} €"


def qty(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def fr_date(value: date) -> str:
    return value.strftime("%d/%m/%Y")


INVOICES: list[Invoice] = [
    Invoice(
        filename="01_facture_conforme_fabrikam.pdf",
        vendor="Fabrikam, Inc.",
        address=["12 rue de la Paix", "75002 Paris"],
        vat="FR40123456789",
        siret="123 456 789 00012",
        color="#1f4e79",
        number="FAB-2026-0412",
        issued=date(2026, 9, 18),
        due=date(2026, 10, 18),
        po_ref="106001",
        lines=[
            Line("1896-S", "ATHENS Desk", 4, 649.40),
            Line("1900-S", "PARIS Guest Chair, black", 8, 125.10),
        ],
    ),
    Invoice(
        filename="02_facture_ecart_montant_wwi.pdf",
        vendor="Wide World Importers",
        address=["Quai du Lazaret, Bât. C", "13002 Marseille"],
        vat="FR83456789012",
        siret="345 678 901 00027",
        color="#7a3b00",
        number="WWI-INV-55821",
        issued=date(2026, 9, 22),
        due=date(2026, 10, 22),
        po_ref="106002",
        lines=[
            Line("1908-S", "LONDON Swivel Chair, blue", 10, 138.10),
            Line("1928-S", "AMSTERDAM Lamp", 5, 35.60),
        ],
        notes=["Hausse tarifaire matières premières appliquée au 01/09/2026."],
    ),
    Invoice(
        filename="03_facture_doublon_first_up.pdf",
        vendor="First Up Consultants",
        address=["8 place Bellecour", "69002 Lyon"],
        vat="FR61234567890",
        siret="612 345 678 00019",
        color="#2e6b30",
        number="FUC-2026-0087",
        issued=date(2026, 9, 15),
        due=date(2026, 10, 15),
        po_ref="106003",
        lines=[Line("PREST-ERP", "Conseil migration ERP (jour-homme)", 3, 950.00)],
        notes=["Relance : merci de procéder au règlement."],
    ),
    Invoice(
        filename="04_facture_fournisseur_inconnu.pdf",
        vendor="Atelier Numérique Bordeaux SARL",
        address=["45 cours de l'Intendance", "33000 Bordeaux"],
        vat="FR11987654321",
        siret="987 654 321 00033",
        color="#5b2a86",
        number="ANB-2026-118",
        issued=date(2026, 9, 25),
        due=date(2026, 10, 25),
        po_ref=None,
        lines=[
            Line("DEV-JH", "Développement module e-commerce (jour)", 5, 720.00),
            Line("HEB-12M", "Hébergement annuel", 1, 480.00),
        ],
    ),
    Invoice(
        filename="05_facture_total_incoherent_gdi.pdf",
        vendor="Graphic Design Institute",
        address=["3 allée Duquesne", "44000 Nantes"],
        vat="FR72345678901",
        siret="723 456 789 00041",
        color="#9c1c4b",
        number="GDI-2026-0309",
        issued=date(2026, 9, 26),
        due=date(2026, 10, 26),
        po_ref="106004",
        lines=[
            Line("CHG-01", "Refonte charte graphique", 1, 1800.00),
            Line("MAQ-CAT", "Maquettes catalogue produits", 2, 450.00),
        ],
        printed_ttc_override=3420.00,  # correct value is 3 240,00 €
    ),
]


def build(invoice: Invoice) -> Path:
    path = OUT_DIR / invoice.filename
    doc = SimpleDocTemplate(
        str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Facture {invoice.number}", author=invoice.vendor,
    )
    styles = getSampleStyleSheet()
    accent = colors.HexColor(invoice.color)
    h_vendor = ParagraphStyle("vendor", parent=styles["Title"], alignment=0, textColor=accent, fontSize=20, spaceAfter=2)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=9, leading=12)
    right = ParagraphStyle("right", parent=small, alignment=TA_RIGHT)
    title = ParagraphStyle("title", parent=styles["Heading1"], textColor=accent, fontSize=18, spaceBefore=6)

    story = [
        Paragraph(invoice.vendor, h_vendor),
        Paragraph("<br/>".join(invoice.address), small),
        Paragraph(f"N° TVA : {invoice.vat} — SIRET : {invoice.siret}", small),
        Spacer(1, 8 * mm),
    ]

    meta = [
        f"Facture N° : <b>{invoice.number}</b>",
        f"Date : {fr_date(invoice.issued)}",
        f"Échéance : {fr_date(invoice.due)}",
    ]
    if invoice.po_ref:
        meta.append(f"Votre commande : {invoice.po_ref}")
    client = "<b>Facturé à :</b><br/>CRONUS France S.A.<br/>Service Comptabilité Fournisseurs<br/>" \
             "5 avenue des Champs<br/>92100 Boulogne-Billancourt"
    header = Table(
        [[Paragraph("<br/>".join(meta), small), Paragraph(client, small)]], colWidths=[85 * mm, 89 * mm]
    )
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (1, 0), (1, 0), 0.5, colors.grey),
                                ("LEFTPADDING", (1, 0), (1, 0), 8), ("TOPPADDING", (1, 0), (1, 0), 6),
                                ("BOTTOMPADDING", (1, 0), (1, 0), 6)]))
    story += [Paragraph("FACTURE", title), header, Spacer(1, 8 * mm)]

    rows = [["Réf.", "Désignation", "Qté", "PU HT", "Montant HT"]]
    rows += [[line.ref, line.description, qty(line.qty), eur(line.unit_price), eur(line.total)] for line in invoice.lines]
    table = Table(rows, colWidths=[24 * mm, 76 * mm, 16 * mm, 28 * mm, 30 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), accent),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")]),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.grey),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [table, Spacer(1, 6 * mm)]

    totals = Table(
        [
            ["Total HT", eur(invoice.subtotal)],
            [f"TVA {invoice.vat_rate * 100:g} %", eur(invoice.tax)],
            ["Total TTC", eur(invoice.ttc)],
        ],
        colWidths=[40 * mm, 34 * mm], hAlign="RIGHT",
    )
    totals.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
        ("LINEABOVE", (0, 2), (-1, 2), 1, accent),
    ]))
    story += [totals, Spacer(1, 10 * mm)]

    for note in invoice.notes:
        story.append(Paragraph(f"<i>{note}</i>", small))
    story += [
        Spacer(1, 4 * mm),
        Paragraph(
            "Conditions de paiement : 30 jours date de facture, par virement. "
            "Pénalités de retard : 3 fois le taux d'intérêt légal. Indemnité forfaitaire pour frais de recouvrement : 40 €.",
            small,
        ),
        Paragraph("IBAN : FR76 3000 4000 0312 3456 7890 143 — BIC : BNPAFRPPXXX", small),
    ]
    doc.build(story)
    return path


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    for invoice in INVOICES:
        print("✔", build(invoice).name)


if __name__ == "__main__":
    main()
