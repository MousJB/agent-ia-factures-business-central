"""OpenAI and Azure OpenAI provider — Chat Completions function calling."""

from __future__ import annotations

import json
from typing import Any

import openai

from app.llm.base import AgentCallbacks, LLMClient, LLMError, ToolSpec


class OpenAIClient(LLMClient):
    provider = "openai"
    supports_native_pdf = False

    def __init__(self, client: openai.OpenAI, model: str, provider: str = "openai") -> None:
        self.client = client
        self.model = model
        self.provider = provider

    @classmethod
    def for_openai(cls, api_key: str, model: str) -> "OpenAIClient":
        if not api_key:
            raise LLMError("OPENAI_API_KEY manquante (voir .env.example).")
        return cls(openai.OpenAI(api_key=api_key, timeout=120.0, max_retries=3), model)

    @classmethod
    def for_azure(cls, endpoint: str, api_key: str, api_version: str, deployment: str) -> "OpenAIClient":
        if not (endpoint and api_key and deployment):
            raise LLMError("AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY et AZURE_OPENAI_DEPLOYMENT sont requis.")
        client = openai.AzureOpenAI(
            azure_endpoint=endpoint, api_key=api_key, api_version=api_version, timeout=120.0, max_retries=3
        )
        # On Azure, "model" is the deployment name.
        return cls(client, deployment, provider="azure_openai")

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
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        tool_defs = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in tools
        ]

        for _ in range(max_iterations):
            try:
                response = self.client.chat.completions.create(
                    model=self.model, messages=messages, tools=tool_defs, tool_choice="auto", temperature=0
                )
            except openai.AuthenticationError as exc:
                raise LLMError("Clé OpenAI / Azure OpenAI invalide.") from exc
            except openai.RateLimitError as exc:
                raise LLMError("Quota OpenAI dépassé (429), réessayer plus tard.") from exc
            except openai.APIStatusError as exc:
                raise LLMError(f"Erreur API OpenAI {exc.status_code} : {exc.message}") from exc
            except openai.APIConnectionError as exc:
                raise LLMError(f"Connexion à l'API OpenAI impossible : {exc}") from exc

            choice = response.choices[0]
            message = choice.message
            tool_calls = message.tool_calls or []
            messages.append(message.model_dump(exclude_none=True))

            if not tool_calls:
                return message.content or ""
            if message.content:
                callbacks.on_thought(message.content)

            for call in tool_calls:
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    result: dict[str, Any] = {"error": "Arguments JSON invalides, renvoyer l'appel corrigé."}
                else:
                    result = callbacks.execute_tool(call.function.name, arguments)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False, default=str)}
                )
            if callbacks.is_done():
                return ""

        raise LLMError(f"Nombre maximal d'itérations atteint ({max_iterations}).")
