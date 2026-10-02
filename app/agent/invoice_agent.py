"""Invoice-entry agent: orchestrates the LLM tool loop for one invoice."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.agent.checks import eur
from app.agent.tools import TOOL_SPECS, InvoiceToolbox
from app.erp.base import ERPClient
from app.llm.base import LLMClient, LLMError
from app.models import AgentStep, InvoiceJob, JobStatus, Severity, StepKind
from app.pdf_utils import PDFError, extract_text

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Tu es un agent de comptabilité fournisseurs intégré à Microsoft Dynamics 365 Business Central.
Ta mission : saisir une facture fournisseur reçue en PDF sous forme de facture d'achat EN BROUILLON,
et signaler toute anomalie au comptable, qui validera ou rejettera.

Procédure :
1. Lis la facture et appelle record_invoice_data avec les données exactes (dates au format YYYY-MM-DD,
   montants tels qu'imprimés, même s'ils te semblent incohérents ; null si un champ est absent).
2. search_vendor : n° de TVA en priorité, sinon le nom.
3. Si le fournisseur existe : check_duplicate_invoice, puis find_purchase_order (avec la référence de
   commande citée sur la facture si elle existe), puis compare_with_purchase_order sur la commande retenue.
4. Si le fournisseur existe et que la facture n'est pas un doublon, crée le brouillon avec
   create_draft_purchase_invoice, même s'il y a des écarts : ils seront revus par le comptable.
   Si le fournisseur est inconnu ou si c'est un doublon, NE crée PAS de brouillon.
5. Termine toujours par submit_final_report : synthèse factuelle en français (chiffres à l'appui)
   et recommandation (validate si aucune anomalie bloquante, review si écarts à vérifier, reject si
   doublon ou fournisseur inconnu).

Avant chaque appel d'outil, explique en une phrase courte ce que tu fais et pourquoi.
Les contrôles (arithmétique, doublons, rapprochement commande) sont calculés par les outils :
appuie-toi sur leurs résultats, n'invente aucun chiffre. Ne valide jamais une facture toi-même.
"""

TOOL_TITLES: dict[str, str] = {
    "record_invoice_data": "Extraction des données de la facture",
    "search_vendor": "Recherche du fournisseur dans l'ERP",
    "check_duplicate_invoice": "Contrôle de doublon",
    "find_purchase_order": "Recherche du bon de commande",
    "compare_with_purchase_order": "Rapprochement facture / commande",
    "create_draft_purchase_invoice": "Création de la facture d'achat (brouillon)",
    "submit_final_report": "Synthèse finale",
}


class _JobCallbacks:
    """Bridges provider loop events to the job timeline."""

    def __init__(self, job: InvoiceJob, toolbox: InvoiceToolbox) -> None:
        self.job = job
        self.toolbox = toolbox

    def add_step(self, kind: StepKind, title: str, content: str = "", **extra: Any) -> None:
        self.job.steps.append(AgentStep(index=len(self.job.steps) + 1, kind=kind, title=title, content=content, **extra))

    def on_thought(self, text: str) -> None:
        text = text.strip()
        if text:
            self.add_step(StepKind.THOUGHT, "Raisonnement", text)

    def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        title = TOOL_TITLES.get(name, name)
        self.add_step(StepKind.TOOL_CALL, title, tool_name=name, payload=arguments)
        try:
            result = self.toolbox.execute(name, arguments)
        except Exception as exc:  # defensive: a tool bug must not kill the loop
            logger.exception("Tool %s crashed", name)
            result = {"error": f"Erreur interne de l'outil : {exc}"}
        kind = StepKind.ERROR if "error" in result else StepKind.TOOL_RESULT
        self.add_step(kind, f"Résultat — {title}", _describe_result(name, result), tool_name=name, payload=result)
        return result

    def is_done(self) -> bool:
        return self.toolbox.done


def _describe_result(name: str, result: dict[str, Any]) -> str:
    if "error" in result:
        return str(result["error"])
    if name == "record_invoice_data":
        check = result["arithmetic_check"]
        return "Totaux cohérents." if check["consistent"] else "Incohérences : " + " ; ".join(result["anomalies"])
    if name == "search_vendor":
        if not result["found"]:
            return "Aucun fournisseur correspondant."
        best = result["best_match"]
        return f"{best['number']} — {best['name']} (score {result['match_score']:.0%})"
    if name == "check_duplicate_invoice":
        return "Doublon détecté !" if result["duplicate"] else "Aucun doublon."
    if name == "find_purchase_order":
        orders = result["purchase_orders"]
        if not orders:
            return result.get("note") or "Aucune commande ouverte."
        return ", ".join(f"{po['number']} ({eur(po['total_excl_tax'])} HT)" for po in orders)
    if name == "compare_with_purchase_order":
        cmp = result["comparison"]
        status = "dans la tolérance" if cmp["within_tolerance"] else "HORS tolérance"
        return f"Écart HT {eur(cmp['gap_excl_tax'], signed=True)} ({status})."
    if name == "create_draft_purchase_invoice":
        return f"Brouillon n° {result['draft_invoice']['number']} créé."
    if name == "submit_final_report":
        return "Rapport transmis."
    return json.dumps(result, ensure_ascii=False)[:300]


class InvoiceAgent:
    def __init__(self, llm: LLMClient, erp: ERPClient, max_iterations: int = 15, default_gl_account: str = "607000") -> None:
        self.llm = llm
        self.erp = erp
        self.max_iterations = max_iterations
        self.default_gl_account = default_gl_account

    def process(self, job: InvoiceJob, pdf_bytes: bytes) -> InvoiceJob:
        started = time.perf_counter()
        toolbox = InvoiceToolbox(job, self.erp, self.default_gl_account)
        callbacks = _JobCallbacks(job, toolbox)
        try:
            text = extract_text(pdf_bytes, require_text=not self.llm.supports_native_pdf)
            if not text:
                text = "(aucun texte extractible : lire le PDF joint)"
            callbacks.add_step(StepKind.THOUGHT, "Lecture du PDF", f"{len(text)} caractères extraits du document.")
            user_text = (
                f"Voici une facture fournisseur reçue ({job.filename}). Texte extrait du PDF :\n"
                f"<invoice_text>\n{text}\n</invoice_text>\nTraite-la selon la procédure."
            )
            final_text = self.llm.run_agent(
                system=SYSTEM_PROMPT,
                user_text=user_text,
                pdf_bytes=pdf_bytes,
                tools=TOOL_SPECS,
                callbacks=callbacks,
                max_iterations=self.max_iterations,
            )
            if final_text.strip() and not toolbox.done:
                callbacks.on_thought(final_text)
        except (PDFError, LLMError) as exc:
            job.error = str(exc)
            callbacks.add_step(StepKind.ERROR, "Traitement interrompu", str(exc))
        except Exception as exc:  # last-resort guard so a job never stays "processing"
            logger.exception("Unexpected failure on job %s", job.id)
            job.error = f"Erreur inattendue : {exc}"
            callbacks.add_step(StepKind.ERROR, "Traitement interrompu", job.error)

        self._finalize(job, toolbox)
        job.duration_seconds = round(time.perf_counter() - started, 1)
        return job

    @staticmethod
    def _finalize(job: InvoiceJob, toolbox: InvoiceToolbox) -> None:
        has_errors = any(a.severity == Severity.ERROR for a in job.anomalies)
        # Guardrail: the model's recommendation cannot be more lenient than the controls.
        if job.recommendation == "validate" and has_errors:
            job.recommendation = "review"
        if job.recommendation is None:
            job.recommendation = "reject" if (job.draft_invoice is None) else ("review" if has_errors else "validate")
        if job.summary is None and job.error is None:
            job.summary = "L'agent s'est arrêté sans rapport final ; vérifier le journal de raisonnement."

        if job.draft_invoice is not None:
            job.status = JobStatus.AWAITING_VALIDATION
        elif job.extracted is not None:
            job.status = JobStatus.BLOCKED
        else:
            job.status = JobStatus.FAILED
        callbacks_step = AgentStep(
            index=len(job.steps) + 1,
            kind=StepKind.FINAL,
            title="En attente de validation humaine" if job.draft_invoice else "Aucun brouillon créé",
            content=job.summary or job.error or "",
        )
        job.steps.append(callbacks_step)
