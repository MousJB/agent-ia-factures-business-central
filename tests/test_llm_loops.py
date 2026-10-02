"""Provider loops (Anthropic / OpenAI) against fake HTTP endpoints — no API key needed."""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx
import httpx2
import openai

from app.llm.anthropic_client import AnthropicClient
from app.llm.base import ToolSpec
from app.llm.openai_client import OpenAIClient

TOOLS = [ToolSpec("search_vendor", "Recherche", {"type": "object", "properties": {"name": {"type": "string"}}})]


class Recorder:
    def __init__(self) -> None:
        self.thoughts: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def on_thought(self, text: str) -> None:
        self.thoughts.append(text)

    def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return {"found": True}

    def is_done(self) -> bool:
        return False


def _anthropic_message(content: list[dict[str, Any]], stop_reason: str) -> dict[str, Any]:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


def test_anthropic_tool_loop() -> None:
    bodies: list[dict[str, Any]] = []
    responses = [
        _anthropic_message(
            [
                {"type": "thinking", "thinking": "Je cherche le fournisseur.", "signature": "sig"},
                {"type": "tool_use", "id": "tu_1", "name": "search_vendor", "input": {"name": "Fabrikam"}},
            ],
            "tool_use",
        ),
        _anthropic_message([{"type": "text", "text": "Terminé."}], "end_turn"),
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        assert "server-side-fallback-2026-07-01" in request.headers.get("anthropic-beta", "")
        return httpx2.Response(200, json=responses[len(bodies) - 1])

    client = AnthropicClient("sk-test", "claude-opus-5-5")
    client.client = anthropic.Anthropic(
        api_key="sk-test", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler))
    )
    recorder = Recorder()
    final = client.run_agent(system="sys", user_text="facture", pdf_bytes=b"%PDF-1.4", tools=TOOLS,
                             callbacks=recorder, max_iterations=5)

    assert final == "Terminé."
    assert recorder.calls == [("search_vendor", {"name": "Fabrikam"})]
    assert recorder.thoughts == ["Je cherche le fournisseur."]
    first, second = bodies
    assert first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default"
    assert first["thinking"]["type"] == "adaptive"
    assert first["messages"][0]["content"][0]["type"] == "document"
    # Thinking block is replayed unchanged, then the tool result follows.
    assert second["messages"][1]["content"][0]["type"] == "thinking"
    assert second["messages"][2]["content"][0]["tool_use_id"] == "tu_1"


def test_openai_tool_loop() -> None:
    bodies: list[dict[str, Any]] = []

    def completion(message: dict[str, Any], finish: str) -> dict[str, Any]:
        return {"id": "c", "object": "chat.completion", "created": 0, "model": "gpt-4.1",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}]}

    responses = [
        completion({"role": "assistant", "content": "Je cherche.", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "search_vendor", "arguments": '{"name": "Fabrikam"}'}}
        ]}, "tool_calls"),
        completion({"role": "assistant", "content": "Fini."}, "stop"),
    ]

    # Recent openai SDKs are built on httpx2; older ones on httpx.
    http_lib = httpx2 if openai.DefaultHttpxClient.__mro__[1].__module__.startswith("httpx2") else httpx

    def handler(request: Any) -> Any:
        bodies.append(json.loads(request.content))
        return http_lib.Response(200, json=responses[len(bodies) - 1])

    http_client = openai.DefaultHttpxClient(transport=http_lib.MockTransport(handler))
    client = OpenAIClient(openai.OpenAI(api_key="sk-test", http_client=http_client), "gpt-4.1")
    recorder = Recorder()
    final = client.run_agent(system="sys", user_text="facture", pdf_bytes=None, tools=TOOLS,
                             callbacks=recorder, max_iterations=5)

    assert final == "Fini."
    assert recorder.calls == [("search_vendor", {"name": "Fabrikam"})]
    assert recorder.thoughts == ["Je cherche."]
    assert bodies[1]["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": '{"found": true}'}
