"""Anthropic (Claude) provider — manual tool-use loop on the Messages API."""

from __future__ import annotations

import base64
import json
from typing import Any

import anthropic

from app.llm.base import AgentCallbacks, LLMClient, LLMError, ToolSpec

_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicClient(LLMClient):
    provider = "anthropic"
    supports_native_pdf = True

    def __init__(self, api_key: str, model: str, effort: str = "medium", fallbacks: bool = True) -> None:
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY manquante (voir .env.example).")
        self.client = anthropic.Anthropic(api_key=api_key, timeout=180.0, max_retries=3)
        self.model = model
        self.effort = effort
        self.fallbacks = fallbacks

    def _create(self, **kwargs: Any) -> Any:
        if self.fallbacks:
            # Server-side fallback: a request declined by safety classifiers is retried on another model.
            return self.client.beta.messages.create(betas=[_FALLBACK_BETA], fallbacks="default", **kwargs)
        return self.client.beta.messages.create(**kwargs)

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
        content: list[dict[str, Any]] = []
        if pdf_bytes:
            content.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
                    },
                }
            )
        content.append({"type": "text", "text": user_text})
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        tool_defs = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]

        for _ in range(max_iterations):
            try:
                response = self._create(
                    model=self.model,
                    max_tokens=16000,
                    system=system,
                    tools=tool_defs,
                    tool_choice={"type": "auto"},
                    thinking={"type": "adaptive", "display": "summarized"},
                    output_config={"effort": self.effort},
                    messages=messages,
                )
            except anthropic.AuthenticationError as exc:
                raise LLMError("Clé Anthropic invalide.") from exc
            except anthropic.RateLimitError as exc:
                raise LLMError("Quota Anthropic dépassé (429), réessayer plus tard.") from exc
            except anthropic.APIStatusError as exc:
                raise LLMError(f"Erreur API Anthropic {exc.status_code} : {exc.message}") from exc
            except anthropic.APIConnectionError as exc:
                raise LLMError(f"Connexion à l'API Anthropic impossible : {exc}") from exc

            if response.stop_reason == "refusal":
                raise LLMError("Le modèle a refusé de traiter ce document.")

            final_text_parts: list[str] = []
            tool_uses = []
            for block in response.content:
                if block.type == "thinking" and getattr(block, "thinking", ""):
                    callbacks.on_thought(block.thinking)
                elif block.type == "text":
                    final_text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_uses.append(block)

            # Keep the full assistant content (incl. thinking blocks) unchanged in history.
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "max_tokens":
                raise LLMError("Réponse tronquée (max_tokens atteint).")
            if not tool_uses:
                return "\n".join(final_text_parts)

            for part in final_text_parts:
                callbacks.on_thought(part)

            results = []
            for block in tool_uses:
                arguments = block.input if isinstance(block.input, dict) else json.loads(block.input)
                result = callbacks.execute_tool(block.name, arguments)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                        "is_error": "error" in result,
                    }
                )
            messages.append({"role": "user", "content": results})
            if callbacks.is_done():
                return ""

        raise LLMError(f"Nombre maximal d'itérations atteint ({max_iterations}).")
