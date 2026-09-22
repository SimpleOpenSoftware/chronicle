#!/usr/bin/env python3
"""
Mock LLM Server - OpenAI-compatible HTTP server for testing.

This server mimics OpenAI's API for chat completions and embeddings without external dependencies.

Architecture:
- HTTP server on 0.0.0.0:11435
- Three endpoints: /v1/chat/completions, /v1/embeddings, /v1/models
- Deterministic responses for reproducible tests

Request Detection:
- Vault memory agent: request carries tools including "write_note" — the mock
  acts as a minimal agent: one write_note tool call recording a conversation
  note whose facts are derived from the actual transcript, then a final
  summary once the tool result comes back. Deriving facts from the input is
  what keeps content assertions (e.g. "a memory mentions the trumpet flower")
  meaningful under the mock profile.
- Fact extraction: system prompt contains "FACT_RETRIEVAL_PROMPT" or "extract facts"
- Memory updates: system prompt contains "UPDATE_MEMORY_PROMPT" or "memory manager"
"""

import argparse
import asyncio
import hashlib
import json
import logging
import re
from typing import List, Optional

import numpy as np
from aiohttp import web

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def generate_deterministic_embedding(text: str, dimensions: int = 1536) -> List[float]:
    """
    Generate deterministic embedding using hash seeding.

    Same text always produces same embedding for reproducible tests.
    Generates unit vector for cosine similarity compatibility.
    """
    # Use SHA-256 hash as seed
    hash_bytes = hashlib.sha256(text.encode("utf-8")).digest()
    seed = int.from_bytes(hash_bytes[:4], "big")

    # Generate reproducible random vector
    rng = np.random.default_rng(seed)
    vector = rng.standard_normal(dimensions)

    # Normalize to unit vector (cosine similarity compatible)
    norm = np.linalg.norm(vector)
    return (vector / norm).tolist()


def detect_request_type(messages: List[dict]) -> str:
    """
    Detect request type by analyzing system prompt.

    Returns:
    - "fact_extraction": For fact retrieval prompts
    - "memory_update": For memory manager prompts
    - "general": For other requests
    """
    if not messages:
        return "general"

    # Check first message (usually system prompt)
    first_message = messages[0].get("content", "").lower()

    # Fact extraction detection
    if "fact_retrieval_prompt" in first_message or "extract facts" in first_message:
        return "fact_extraction"

    # Memory update detection
    if "update_memory_prompt" in first_message or "memory manager" in first_message:
        return "memory_update"

    return "general"


def _task_field(task: str, name: str) -> Optional[str]:
    """Read one 'name: value' line from the memory agent's task message."""
    match = re.search(rf"^{name}:\s*(.+)$", task, re.MULTILINE)
    return match.group(1).strip() if match else None


def derive_facts_from_transcript(transcript: str) -> List[str]:
    """Distill a speaker-labelled transcript into one fact per sentence.

    Deterministic stand-in for LLM extraction: strip speaker labels, split into
    sentences, keep the substantive ones verbatim. Because facts come from the
    real input, content assertions hold the same way they would against a real
    provider's extraction.
    """
    text = " ".join(transcript.split())
    text = re.sub(r"(?:^|\s)(?:[A-Z]|Speaker[ _]?\d+):\s+", " ", text)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return [s.strip() for s in sentences if len(s.strip()) >= 8]


def create_vault_agent_response(messages: List[dict]) -> dict:
    """Act as a minimal vault memory agent.

    Round 1: one write_note tool call creating Conversations/<id>.md in the
    note-template shape, with facts derived from the transcript. Round 2 (a
    tool result is present): a plain completion so the agent loop finishes.
    """
    if any(m.get("role") == "tool" for m in messages):
        return create_general_response("Recorded the conversation note.")

    task = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
    conversation_id = _task_field(task, "conversation_id") or "unknown-conversation"
    date = _task_field(task, "date") or "1970-01-01T00:00:00"
    duration = _task_field(task, "duration_minutes") or "unknown"
    title = _task_field(task, "source_title") or "Conversation"
    transcript = task.split("Transcript (speaker-labelled):", 1)[-1].strip()

    facts = derive_facts_from_transcript(transcript) or ["No transcript content."]
    summary = " ".join(facts[:2])
    note = "\n".join(
        [
            "---",
            "categories:",
            '  - "[[Conversations]]"',
            f"conversation_id: {json.dumps(conversation_id)}",
            f"date: {json.dumps(date)}",
            "people: []",
            "topics: []",
            f"duration_minutes: {duration if duration != 'unknown' else ''}",
            "---",
            f"## {title}",
            "",
            "### Summary",
            summary,
            "",
            "### Key Facts",
            *[f"- {fact}" for fact in facts],
            "",
            "### Action Items",
            "- [ ]",
            "",
        ]
    )

    return {
        "id": "chatcmpl-mock-vault-agent",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-mock-write-note",
                            "type": "function",
                            "function": {
                                "name": "write_note",
                                "arguments": json.dumps(
                                    {
                                        "path": f"Conversations/{conversation_id}.md",
                                        "content": note,
                                        "overwrite": True,
                                    }
                                ),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 200, "completion_tokens": 150, "total_tokens": 350},
    }


def create_fact_extraction_response() -> dict:
    """Create fact extraction response (JSON format)."""
    facts = [
        "User likes hiking",
        "User met with John",
        "Discussed project timeline",
        "User prefers morning meetings",
        "User is working on Chronicle project",
    ]

    content = json.dumps({"facts": facts})

    return {
        "id": "chatcmpl-mock-fact",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


def create_memory_update_response() -> dict:
    """
    Create memory update response (XML format).

    Supports multiple XML formats:
    - Plain XML: <result>...</result>
    - Markdown code blocks: ```xml ... ```
    - DeepSeek think tags: <think>...</think><result>...</result>
    """
    # Plain XML format (most common)
    xml_content = """<result>
  <memory>
    <item id="0" event="UPDATE">
      <text>User likes hiking in the mountains</text>
      <old_memory>User likes hiking</old_memory>
    </item>
    <item id="1" event="ADD">
      <text>User prefers morning meetings before 10am</text>
    </item>
  </memory>
</result>"""

    return {
        "id": "chatcmpl-mock-memory",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": xml_content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 150, "completion_tokens": 80, "total_tokens": 230},
    }


def create_general_response(user_message: str, json_mode: bool = False) -> dict:
    """Create general chat completion response.

    When the caller asked for response_format={"type": "json_object"} it will
    json.loads() whatever comes back, so prose is not a valid answer -- honour the
    contract the real API honours. The affirmative shape keeps LLM-as-judge
    verification keywords working against the stub.
    """
    if json_mode:
        response_text = json.dumps(
            {
                "similar": True,
                "score": 0.9,
                "reason": "Mock LLM: json_object response_format requested.",
            }
        )
    else:
        response_text = f"This is a mock response to: {user_message}"

    return {
        "id": "chatcmpl-mock-general",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
    }


def create_chat_response(data: dict) -> dict:
    """Build one canonical deterministic result for JSON and streaming callers."""
    messages = data.get("messages", [])
    json_mode = (data.get("response_format") or {}).get("type") == "json_object"
    tool_names = {
        (tool.get("function") or {}).get("name") for tool in data.get("tools") or []
    }
    if "write_note" in tool_names:
        return create_vault_agent_response(messages)
    request_type = detect_request_type(messages)
    logger.info("Chat completion request detected as: %s", request_type)
    if request_type == "fact_extraction":
        return create_fact_extraction_response()
    if request_type == "memory_update":
        return create_memory_update_response()
    content = messages[-1].get("content", "") if messages else ""
    return create_general_response(content, json_mode=json_mode)


def completion_chunks(response: dict, *, include_usage: bool = False):
    """Exercise real SDK delta assembly, including split tool argument JSON."""
    base = {key: response[key] for key in ("id", "created", "model")}
    base["object"] = "chat.completion.chunk"
    for choice in response["choices"]:
        index = choice["index"]
        message = choice["message"]

        def chunk(delta, finish_reason=None):
            return {
                **base,
                "choices": [
                    {"index": index, "delta": delta, "finish_reason": finish_reason}
                ],
            }

        yield chunk({"role": message["role"]})
        content = message.get("content") or ""
        for offset in range(0, len(content), 16):
            yield chunk({"content": content[offset : offset + 16]})
        for tool_index, call in enumerate(message.get("tool_calls") or []):
            function = call["function"]
            yield chunk(
                {
                    "tool_calls": [
                        {
                            "index": tool_index,
                            "id": call["id"],
                            "type": call["type"],
                            "function": {"name": function["name"], "arguments": ""},
                        }
                    ]
                }
            )
            arguments = function["arguments"]
            for offset in range(0, len(arguments), 16):
                yield chunk(
                    {
                        "tool_calls": [
                            {
                                "index": tool_index,
                                "function": {
                                    "arguments": arguments[offset : offset + 16]
                                },
                            }
                        ]
                    }
                )
        yield chunk({}, choice["finish_reason"])
    if include_usage and "usage" in response:
        yield {**base, "choices": [], "usage": response["usage"]}


async def stream_chat_response(
    request: web.Request, completion: dict, *, include_usage: bool
):
    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
        }
    )
    await response.prepare(request)
    try:
        for chunk in completion_chunks(completion, include_usage=include_usage):
            await response.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            await asyncio.sleep(
                0
            )  # Let other requests run while the SDK consumes deltas.
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
    except ConnectionResetError:
        logger.info("Streaming chat client disconnected")
    return response


async def handle_chat_completions(request: web.Request) -> web.StreamResponse:
    """Honor the same OpenAI JSON/SSE contract as the real profile providers."""
    try:
        data = await request.json()
        response = create_chat_response(data)
        if data.get("stream"):
            return await stream_chat_response(
                request,
                response,
                include_usage=bool(
                    (data.get("stream_options") or {}).get("include_usage")
                ),
            )
        return web.json_response(response)
    except Exception as error:
        logger.error("Error handling chat completions: %s", error, exc_info=True)
        return web.json_response(
            {"error": {"message": str(error), "type": "server_error"}}, status=500
        )


async def handle_embeddings(request: web.Request) -> web.Response:
    """Handle /v1/embeddings endpoint."""
    try:
        data = await request.json()
        input_texts = data.get("input", [])

        # Ensure input is a list
        if isinstance(input_texts, str):
            input_texts = [input_texts]

        # Generate deterministic embeddings
        embeddings_data = []
        for idx, text in enumerate(input_texts):
            embedding = generate_deterministic_embedding(text, dimensions=1536)
            embeddings_data.append(
                {"object": "embedding", "embedding": embedding, "index": idx}
            )

        logger.info(f"Generated {len(embeddings_data)} embeddings")

        response = {
            "object": "list",
            "data": embeddings_data,
            "model": "text-embedding-3-small",
            "usage": {
                "prompt_tokens": len(input_texts) * 10,
                "total_tokens": len(input_texts) * 10,
            },
        }

        return web.json_response(response)

    except Exception as e:
        logger.error(f"Error handling embeddings: {e}", exc_info=True)
        return web.json_response(
            {"error": {"message": str(e), "type": "server_error"}}, status=500
        )


async def handle_models(request: web.Request) -> web.Response:
    """Handle /v1/models endpoint."""
    response = {
        "object": "list",
        "data": [
            {
                "id": "gpt-4o-mini",
                "object": "model",
                "created": 1234567890,
                "owned_by": "mock-llm",
            },
            {
                "id": "text-embedding-3-small",
                "object": "model",
                "created": 1234567890,
                "owned_by": "mock-llm",
            },
        ],
    }

    logger.info("Returning available models")
    return web.json_response(response)


async def handle_health(request: web.Request) -> web.Response:
    """Handle health check endpoint."""
    return web.json_response({"status": "healthy"})


def create_app() -> web.Application:
    """Create aiohttp application with routes."""
    app = web.Application()

    # OpenAI-compatible routes
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_post("/v1/embeddings", handle_embeddings)
    app.router.add_get("/v1/models", handle_models)

    # Health check
    app.router.add_get("/health", handle_health)

    return app


def main(host: str, port: int):
    """Start HTTP server."""
    logger.info(f"Starting Mock LLM Server on {host}:{port}")
    logger.info(f"OpenAI-compatible endpoints:")
    logger.info(f"  - POST /v1/chat/completions")
    logger.info(f"  - POST /v1/embeddings")
    logger.info(f"  - GET /v1/models")
    logger.info(f"  - GET /health")
    logger.info(f"Deterministic embeddings: 1536 dimensions")

    app = create_app()
    web.run_app(app, host=host, port=port, access_log=logger)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock LLM Server")
    parser.add_argument(
        "--host", default="0.0.0.0", help="Server host (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=11435, help="Server port (default: 11435)"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    try:
        main(args.host, args.port)
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
