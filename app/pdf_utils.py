"""PDF text extraction."""

from __future__ import annotations

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_PDF_BYTES = 10 * 1024 * 1024


class PDFError(Exception):
    """Raised when the uploaded file is not a readable PDF."""


def validate_pdf(data: bytes) -> None:
    if not data:
        raise PDFError("Fichier vide.")
    if len(data) > MAX_PDF_BYTES:
        raise PDFError("Fichier trop volumineux (10 Mo max).")
    if not data.lstrip()[:5].startswith(b"%PDF"):
        raise PDFError("Le fichier n'est pas un PDF.")


def extract_text(data: bytes, require_text: bool = True) -> str:
    validate_pdf(data)
    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except (PdfReadError, ValueError, KeyError) as exc:
        raise PDFError(f"PDF illisible : {exc}") from exc
    if require_text and len(text) < 20:
        raise PDFError(
            "Aucun texte exploitable dans le PDF (document scanné ?). "
            "Utiliser le mode Anthropic, qui lit le PDF nativement, ou ajouter une étape OCR."
        )
    return text
