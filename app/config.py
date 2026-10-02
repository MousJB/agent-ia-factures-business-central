"""Application settings loaded from environment variables (.env supported)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    erp_mode: str
    llm_provider: str
    agent_max_iterations: int

    anthropic_api_key: str
    anthropic_model: str
    anthropic_effort: str
    anthropic_fallbacks: bool

    openai_api_key: str
    openai_model: str

    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_api_version: str
    azure_openai_deployment: str

    bc_tenant_id: str
    bc_client_id: str
    bc_client_secret: str
    bc_environment: str
    bc_company_id: str
    bc_company_name: str
    bc_default_gl_account: str

    data_dir: Path
    samples_dir: Path
    uploads_dir: Path

    @property
    def llm_model_label(self) -> str:
        return {
            "anthropic": self.anthropic_model,
            "openai": self.openai_model,
            "azure_openai": f"azure:{self.azure_openai_deployment}",
            "offline": "simulateur déterministe (sans IA)",
        }.get(self.llm_provider, "?")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        erp_mode=_env("ERP_MODE", "mock").lower(),
        llm_provider=_env("LLM_PROVIDER", "offline").lower(),
        agent_max_iterations=int(_env("AGENT_MAX_ITERATIONS", "15") or 15),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        anthropic_model=_env("ANTHROPIC_MODEL", "claude-opus-5-5"),
        anthropic_effort=_env("ANTHROPIC_EFFORT", "medium"),
        anthropic_fallbacks=_env_bool("ANTHROPIC_FALLBACKS", True),
        openai_api_key=_env("OPENAI_API_KEY"),
        openai_model=_env("OPENAI_MODEL", "gpt-4.1"),
        azure_openai_endpoint=_env("AZURE_OPENAI_ENDPOINT"),
        azure_openai_api_key=_env("AZURE_OPENAI_API_KEY"),
        azure_openai_api_version=_env("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        azure_openai_deployment=_env("AZURE_OPENAI_DEPLOYMENT"),
        bc_tenant_id=_env("BC_TENANT_ID"),
        bc_client_id=_env("BC_CLIENT_ID"),
        bc_client_secret=_env("BC_CLIENT_SECRET"),
        bc_environment=_env("BC_ENVIRONMENT", "Production"),
        bc_company_id=_env("BC_COMPANY_ID"),
        bc_company_name=_env("BC_COMPANY_NAME"),
        bc_default_gl_account=_env("BC_DEFAULT_GL_ACCOUNT", "607000"),
        data_dir=BASE_DIR / "data",
        samples_dir=BASE_DIR / "samples",
        uploads_dir=BASE_DIR / "uploads",
    )
