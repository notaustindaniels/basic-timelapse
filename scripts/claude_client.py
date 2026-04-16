"""
Claude client — uses the Claude Agent SDK, which reads CLAUDE_CODE_OAUTH_TOKEN
from the environment. That token is generated via `claude setup-token` (see
README.md) and authenticates programmatic calls against the user's Claude
subscription — no separate API credit balance required.

Public function: generate_prompts(intent, mode, hero_image_path=None) -> dict

Returns the parsed JSON structure documented in
references/restoration_system_prompt.md.

Hero mode attaches the rendered hero image as a base64 image block in the
user turn so Claude can actually SEE the composition and reverse-engineer
the earlier scenes from it. Without that attachment, hero mode would be
no better than direct mode with a wasted image-gen call.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from pathlib import Path
from typing import AsyncIterator, Optional

from claude_agent_sdk import ClaudeAgentOptions, query

from config import CLAUDE_MODEL

log = logging.getLogger(__name__)

SKILL_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT_PATH = SKILL_ROOT / "references" / "restoration_system_prompt.md"


def _load_system_prompt() -> str:
    """Read the system prompt fresh on every call — so edits don't require a redeploy."""
    return SYSTEM_PROMPT_PATH.read_text()


def _build_user_message_text(
    intent: str,
    mode: str,
    has_hero_image: bool,
) -> str:
    """
    Build the TEXT portion of the user-role message sent to Claude.

    The image (if any) is attached as a separate content block; this function
    only returns the text that accompanies it.
    """
    lines = [
        f"User intent: {intent}",
        "",
        f"Mode: {mode}",
        "",
    ]

    if has_hero_image:
        lines.extend([
            "A hero concept image is attached above. Treat it as the TARGET for "
            "Phase A, Scene 4 (the final above-ground staged shot). Study the "
            "image's camera angle, framing, lighting, landmarks, and staging, "
            "then reverse-engineer Phase A Scenes 1-3 as earlier stages of the "
            "transformation that lead naturally into this exact final image. "
            "For Phase B, show what's hidden below/inside — anchored to this "
            "same camera geometry.",
            "",
        ])
    else:
        lines.extend([
            "No hero image is attached. Build Phase A from the intent alone, then "
            "build Phase B so its Scene 1 feels like a continuation of Phase A's "
            "Scene 4 (same geometry, new vantage point inside or below).",
            "",
        ])

    lines.extend([
        "Respond with a single JSON object exactly matching the output contract in "
        "the system prompt. No markdown, no preamble, no trailing text.",
    ])

    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    """
    Claude occasionally wraps JSON in ```json fences despite instructions.
    Strip them, then parse.
    """
    text = text.strip()

    # Strip ```json ... ``` or ``` ... ``` fences
    fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # Try to find the first { ... } block
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            return json.loads(brace_match.group(0))
        raise RuntimeError(
            f"Claude returned non-JSON output: {text[:500]!r}"
        ) from e


async def _build_prompt_generator(
    user_text: str,
    image_path: Optional[Path],
) -> AsyncIterator[dict]:
    """
    Build an async generator that yields a single user message, optionally with
    an image attachment. This is the form ClaudeAgentOptions.query() accepts
    for multimodal input — a stream of message dicts in the Messages API
    content-block format.
    """
    content: list[dict] = []

    if image_path is not None and image_path.exists():
        image_bytes = image_path.read_bytes()
        image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        # PNG is what Nano Banana 2 produces by default in this pipeline.
        media_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": image_b64,
            },
        })
        log.info(
            "Attaching hero image to Claude prompt: %s (%d bytes base64)",
            image_path, len(image_b64),
        )

    content.append({"type": "text", "text": user_text})

    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
    }


async def _run_query(
    system_prompt: str,
    user_text: str,
    image_path: Optional[Path] = None,
) -> str:
    """
    Run a single non-agentic Claude query via the Agent SDK.

    When `image_path` is provided, the image is attached as a base64 content
    block in the user turn. When it's None, the call is pure text.

    We don't want tools or file access here — just a plain completion. The SDK
    streams a series of messages; we concatenate the assistant text blocks.
    """
    options = ClaudeAgentOptions(
        model=CLAUDE_MODEL,
        system_prompt=system_prompt,
        # No tools — this is a pure text generation call, not an agent loop.
        allowed_tools=[],
        max_turns=1,
    )

    # If we have an image, we must use the async-generator prompt form.
    # If not, the simpler string-prompt form works too, but we use the generator
    # form uniformly for consistency.
    prompt_gen = _build_prompt_generator(user_text, image_path)

    chunks: list[str] = []
    async for message in query(prompt=prompt_gen, options=options):
        # The Agent SDK yields AssistantMessage objects containing content blocks.
        # We extract text blocks and ignore anything else (status, usage, etc.).
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


def generate_prompts(
    intent: str,
    mode: str,
    hero_image_path: Optional[Path] = None,
) -> dict:
    """
    Call Claude to generate the full prompt bundle.

    Args:
        intent: free-form user description of the subject & vibe
        mode: "direct" | "hero_reverse"
        hero_image_path: path to the hero concept PNG (only used when mode="hero_reverse")

    Returns:
        dict matching the JSON contract in references/restoration_system_prompt.md
    """
    if mode not in ("direct", "hero_reverse"):
        raise ValueError(f"Invalid mode: {mode!r}. Use 'direct' or 'hero_reverse'.")

    system_prompt = _load_system_prompt()

    has_hero = mode == "hero_reverse" and hero_image_path is not None and hero_image_path.exists()
    user_text = _build_user_message_text(intent, mode, has_hero_image=has_hero)

    if mode == "hero_reverse" and not has_hero:
        log.warning(
            "hero_reverse mode requested but hero_image_path is missing or "
            "doesn't exist (%s). Falling back to text-only prompt — output "
            "quality will be equivalent to direct mode.",
            hero_image_path,
        )

    log.info("Calling Claude for prompt generation (mode=%s, image=%s)", mode, has_hero)
    image_to_attach = hero_image_path if has_hero else None
    raw = asyncio.run(_run_query(system_prompt, user_text, image_to_attach))
    log.debug("Claude raw response: %s", raw[:500])

    parsed = _extract_json(raw)
    _validate_shape(parsed)
    return parsed


def generate_hero_concept_prompt(intent: str) -> str:
    """
    Quick helper: ask Claude for ONLY a hero concept prompt (used when mode='hero').

    Returns a single text-to-image prompt string.
    """
    system = _load_system_prompt()
    user_text = (
        f"User intent: {intent}\n\n"
        "Generate ONLY a text-to-image prompt for a single hero/reference image "
        "showing the final staged Phase-A-Scene-4 (above-ground, fully revealed, "
        "viral-ready). No JSON, no scene chain, no animation prompts. "
        "Just the prompt text, ready to paste into an image model. "
        "Include camera angle, lighting, landmarks, and final-state staging. "
        "Keep it to 2-4 sentences."
    )

    raw = asyncio.run(_run_query(system, user_text, image_path=None)).strip()
    # Strip surrounding quotes or fences if Claude wrapped the prompt
    raw = re.sub(r"^```(?:text)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    raw = raw.strip('"').strip("'")
    return raw


# --- Validation ---------------------------------------------------------------

def _validate_shape(data: dict) -> None:
    """Fail loud if Claude's JSON doesn't match the contract."""
    required_top = ["hero_concept_prompt", "phase_a", "phase_b"]
    for k in required_top:
        if k not in data:
            raise RuntimeError(f"Claude output missing required key {k!r}. Got: {list(data.keys())}")

    for phase_key in ("phase_a", "phase_b"):
        phase = data[phase_key]
        scenes = phase.get("scene_prompts", [])
        anims = phase.get("animation_prompts", [])
        if len(scenes) != 4:
            raise RuntimeError(
                f"{phase_key}.scene_prompts must have exactly 4 entries, got {len(scenes)}"
            )
        if len(anims) != 3:
            raise RuntimeError(
                f"{phase_key}.animation_prompts must have exactly 3 entries, got {len(anims)}"
            )
