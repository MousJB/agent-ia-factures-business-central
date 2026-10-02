"""FastAPI entry point: upload, agent processing, human validation."""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent.invoice_agent import InvoiceAgent
from app.config import Settings, get_settings
from app.dependencies import ConfigurationError, get_erp, get_llm
from app.erp.base import ERPClient, ERPError
from app.models import AgentStep, InvoiceJob, JobStatus, StepKind
from app.pdf_utils import PDFError, validate_pdf

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("invoice-agent")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="Agent IA — saisie de factures fournisseurs",
    description="Démo : extraction LLM, contrôles, brouillon Business Central, validation humaine.",
    version="1.0.0",
)


# --------------------------------------------------------------------------- #
# In-memory job store (sufficient for a demo; use a database in production)
# --------------------------------------------------------------------------- #
class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, InvoiceJob] = {}
        self._pdfs: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def add(self, job: InvoiceJob, pdf: bytes) -> None:
        with self._lock:
            self._jobs[job.id] = job
            self._pdfs[job.id] = pdf

    def get(self, job_id: str) -> InvoiceJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Traitement introuvable.")
        return job

    def pdf(self, job_id: str) -> bytes:
        self.get(job_id)
        return self._pdfs[job_id]

    def list(self) -> list[InvoiceJob]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()
            self._pdfs.clear()


store = JobStore()


def erp_dep() -> ERPClient:
    try:
        return get_erp()
    except (ConfigurationError, ERPError) as exc:
        raise HTTPException(status_code=503, detail=f"ERP indisponible : {exc}") from exc


def agent_dep(erp: ERPClient = Depends(erp_dep), settings: Settings = Depends(get_settings)) -> InvoiceAgent:
    try:
        llm = get_llm()
    except ConfigurationError as exc:
        raise HTTPException(status_code=503, detail=f"LLM non configuré : {exc}") from exc
    return InvoiceAgent(llm, erp, settings.agent_max_iterations, settings.bc_default_gl_account)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/api/config")
def config(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {
        "erp_mode": settings.erp_mode,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model_label,
        "company": settings.bc_company_name if settings.erp_mode == "business_central" else "CRONUS (données mock)",
    }


def _start_job(filename: str, pdf: bytes, agent: InvoiceAgent, settings: Settings, tasks: BackgroundTasks) -> InvoiceJob:
    try:
        validate_pdf(pdf)
    except PDFError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    job = InvoiceJob(
        id=uuid.uuid4().hex[:12], filename=filename, llm_provider=settings.llm_provider, erp_mode=settings.erp_mode
    )
    store.add(job, pdf)
    tasks.add_task(agent.process, job, pdf)  # runs in the threadpool; the UI polls the job
    logger.info("Job %s started for %s", job.id, filename)
    return job


@app.post("/api/invoices", status_code=202)
async def upload_invoice(
    tasks: BackgroundTasks,
    file: UploadFile = File(...),
    agent: InvoiceAgent = Depends(agent_dep),
    settings: Settings = Depends(get_settings),
) -> InvoiceJob:
    pdf = await file.read()
    return _start_job(file.filename or "facture.pdf", pdf, agent, settings, tasks)


@app.get("/api/samples")
def list_samples(settings: Settings = Depends(get_settings)) -> list[str]:
    return sorted(p.name for p in settings.samples_dir.glob("*.pdf"))


@app.post("/api/samples/{name}", status_code=202)
def process_sample(
    name: str,
    tasks: BackgroundTasks,
    agent: InvoiceAgent = Depends(agent_dep),
    settings: Settings = Depends(get_settings),
) -> InvoiceJob:
    path = (settings.samples_dir / name).resolve()
    if path.parent != settings.samples_dir.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="Facture d'exemple introuvable.")
    return _start_job(name, path.read_bytes(), agent, settings, tasks)


@app.get("/api/invoices")
def list_jobs() -> list[InvoiceJob]:
    return store.list()


@app.get("/api/invoices/{job_id}")
def get_job(job_id: str) -> InvoiceJob:
    return store.get(job_id)


@app.get("/api/invoices/{job_id}/pdf")
def get_pdf(job_id: str) -> Response:
    return Response(store.pdf(job_id), media_type="application/pdf")


class Decision(BaseModel):
    comment: str | None = None


def _human_step(job: InvoiceJob, title: str, comment: str | None) -> None:
    job.human_comment = comment
    job.steps.append(AgentStep(index=len(job.steps) + 1, kind=StepKind.HUMAN, title=title, content=comment or ""))


@app.post("/api/invoices/{job_id}/validate")
def validate_invoice(job_id: str, decision: Decision | None = None, erp: ERPClient = Depends(erp_dep)) -> InvoiceJob:
    job = store.get(job_id)
    if job.status != JobStatus.AWAITING_VALIDATION or job.draft_invoice is None:
        raise HTTPException(status_code=409, detail="Aucun brouillon en attente de validation pour ce traitement.")
    try:
        job.draft_invoice = erp.post_purchase_invoice(job.draft_invoice.id)
    except ERPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    job.status = JobStatus.VALIDATED
    _human_step(job, f"Validée par le comptable — facture {job.draft_invoice.number} comptabilisée", decision and decision.comment)
    return job


@app.post("/api/invoices/{job_id}/reject")
def reject_invoice(job_id: str, decision: Decision | None = None, erp: ERPClient = Depends(erp_dep)) -> InvoiceJob:
    job = store.get(job_id)
    if job.status not in (JobStatus.AWAITING_VALIDATION, JobStatus.BLOCKED, JobStatus.FAILED):
        raise HTTPException(status_code=409, detail=f"Impossible de rejeter un traitement au statut {job.status.value}.")
    if job.draft_invoice is not None and job.draft_invoice.status == "Draft":
        try:
            erp.delete_draft_purchase_invoice(job.draft_invoice.id)
        except ERPError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    job.status = JobStatus.REJECTED
    _human_step(job, "Rejetée par le comptable — brouillon supprimé" if job.draft_invoice else "Rejetée par le comptable",
                decision and decision.comment)
    return job


@app.post("/api/demo/reset")
def reset_demo(erp: ERPClient = Depends(erp_dep)) -> dict[str, str]:
    reset = getattr(erp, "reset", None)
    if reset is None:
        raise HTTPException(status_code=400, detail="Réinitialisation disponible uniquement en mode mock.")
    reset()
    store.clear()
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
