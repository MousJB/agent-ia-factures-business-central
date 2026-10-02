"""Provider-neutral contract for running a tool-calling agent loop."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol


class LLMError(Exception):
    """Raised when the LLM provider fails (auth, network, refusal, loop limit...)."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema of the arguments


class AgentCallbacks(Protocol):
    """Implemented by the orchestrator; called by the provider loop."""

    def on_thought(self, text: str) -> None:
        """Model reasoning / narration to show in the timeline."""

    def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run a tool. Must never raise: errors are returned as {"error": ...}."""

    def is_done(self) -> bool:
        """True once the agent submitted its final report."""


class LLMClient(ABC):
    provider: str = "abstract"
    supports_native_pdf: bool = False  # True if the raw PDF is sent to the model (scans OK)

    @abstractmethod
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
        """Drive the tool loop until the model stops or ``callbacks.is_done()``.

        Returns the model's final text (may be empty).
        """
