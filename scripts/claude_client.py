"""
Claude client — sends a system prompt + user message (+ optional image) to
Claude via the Agent SDK and returns the raw text response.

Architectural role: this module does NOT know about the Restoration Timelapse
format, IMAGE/VIDEO blocks, phases, or anything specific to the timelapse
workflow. It is a thin wrapper over the Agent SDK's streaming query() that:

  1. Loads a system prompt from a file path (so edits to the file take effect
     on every call without needing a redeploy).
  2. Builds an async-generator prompt containing a single user turn with
     optional image attachment as a base64 content block.
  3. Collects the assistant's text blocks into one string and returns it.

The parser (scripts/prompt_parser.py) is responsible for extracting structured
data from the response. Separation of concerns: this file handles
authentication and transport; the parser handles format.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import AsyncIterator, Optional

from claude_agent_sdk import ClaudeAgentOptions, query

from config import CLAUDE_MODEL

log = logging.getLogger(__name__)


def load_system_prompt(path: Path) -> str:
    """Read a system prompt file fresh. Call this on every invocation so
    edits to the file take effect without needing a reimport.
    """
    return path.read_text()


async def _build_user_turn_generator(
    user_text: str,
    image_path: Optional[Path] = None,
) -> AsyncIterator[dict]:
    """
    Yield exactly one user-role message for the Agent SDK's streaming query().

    When `image_path` is given, the image is attached as a base64 `image`
    content block before the text block. This matches the Anthropic Messages
    API content-block format, which the Agent SDK forwards transparently.
    """
    content: list[dict] = []

    if image_path is not None and image_path.exists():
        image_bytes = image_path.read_bytes()
        image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        suffix = image_path.suffix.lower()
        media_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(suffix, "image/png")
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": image_b64,
            },
        })
        log.info(
            "Attaching image to user turn: %s (%d bytes raw, %d base64)",
            image_path, len(image_bytes), len(image_b64),
        )
    elif image_path is not None:
        log.warning(
            "image_path=%s was passed but file doesn't exist; proceeding "
            "with text-only turn",
            image_path,
        )

    content.append({"type": "text", "text": user_text})

    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
    }


async def _run_query_async(
    system_prompt: str,
    user_text: str,
    image_path: Optional[Path] = None,
) -> str:
    """
    Single non-agentic Claude call. Returns the concatenated assistant text.

    No tools are enabled — this is a pure completion, not an agent loop.
    """
    options = ClaudeAgentOptions(
        model=CLAUDE_MODEL,
        system_prompt=system_prompt,
        allowed_tools=[],
        max_turns=1,
    )

    prompt_gen = _build_user_turn_generator(user_text, image_path)

    chunks: list[str] = []
    async for message in query(prompt=prompt_gen, options=options):
        # The SDK yields AssistantMessage-shaped objects. We extract text
        # blocks and ignore anything else (status messages, usage info, etc).
        content = getattr(message, "content", None)
        if content is None:
            continue
        if isinstance(content, str):
            chunks.append(content)
            continue
        if isinstance(content, list):
            for block in content:
                text = getattr(block, "text", None)
                if text:
                    chunks.append(text)

    return "".join(chunks)


def send_prompt(
    system_prompt: str,
    user_text: str,
    image_path: Optional[Path] = None,
) -> str:
    """
    Blocking wrapper around _run_query_async. This is the main public entry.

    Args:
        system_prompt: the full system prompt string to send in
            ClaudeAgentOptions.system_prompt. Callers typically load this from
            a file via load_system_prompt() — but the choice of which file (and
            whether to pre-modify its contents) belongs to the caller, not here.
        user_text: the text portion of the user turn.
        image_path: optional path to an image to attach as a base64 content
            block before the text. PNG, JPEG, WebP, GIF supported.

    Returns:
        The raw text content of Claude's response, concatenated across any
        streamed blocks. Downstream parsing is the caller's problem.
    """
    log.info(
        "Calling Claude (model=%s, text_chars=%d, image=%s)",
        CLAUDE_MODEL, len(user_text), image_path is not None,
    )
    response = asyncio.run(_run_query_async(system_prompt, user_text, image_path))
    log.info("Claude returned %d chars", len(response))
    log.debug("First 500 chars of response: %s", response[:500])
    return response
