"""Builds the ERP and LLM clients selected by environment variables."""

from __future__ import annotations

from functools import lru_cache

from app.config import Settings, get_settings
from app.erp.base import ERPClient
from app.llm.base import LLMClient, LLMError


class ConfigurationError(Exception):
    """Invalid ERP_MODE / LLM_PROVIDER configuration."""


def build_erp(settings: Settings) -> ERPClient:
    if settings.erp_mode == "mock":
        from app.erp.mock_erp import MockERP

        return MockERP(settings.data_dir)
    if settings.erp_mode == "business_central":
        from app.erp.business_central import BusinessCentralERP

        return BusinessCentralERP.from_settings(settings)
    raise ConfigurationError(f"ERP_MODE inconnu : {settings.erp_mode!r} (mock | business_central)")


def build_llm(settings: Settings) -> LLMClient:
    provider = settings.llm_provider
    if provider == "offline":
        from app.llm.offline_client import OfflineClient

        return OfflineClient()
    if provider == "anthropic":
        from app.llm.anthropic_client import AnthropicClient

        return AnthropicClient(
            settings.anthropic_api_key, settings.anthropic_model, settings.anthropic_effort, settings.anthropic_fallbacks
        )
    if provider == "openai":
        from app.llm.openai_client import OpenAIClient

        return OpenAIClient.for_openai(settings.openai_api_key, settings.openai_model)
    if provider == "azure_openai":
        from app.llm.openai_client import OpenAIClient

        return OpenAIClient.for_azure(
            settings.azure_openai_endpoint,
            settings.azure_openai_api_key,
            settings.azure_openai_api_version,
            settings.azure_openai_deployment,
        )
    raise ConfigurationError(f"LLM_PROVIDER inconnu : {provider!r} (offline | anthropic | openai | azure_openai)")


@lru_cache(maxsize=1)
def get_erp() -> ERPClient:
    return build_erp(get_settings())


@lru_cache(maxsize=1)
def get_llm() -> LLMClient:
    try:
        return build_llm(get_settings())
    except LLMError as exc:
        raise ConfigurationError(str(exc)) from exc
