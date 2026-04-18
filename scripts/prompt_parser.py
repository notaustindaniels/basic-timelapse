"""
prompt_parser.py — Deterministic regex parser for the Restoration Timelapse
custom GPT's output format.

The GPT's system prompt (references/restoration_system_prompt.md) mandates a
specific markdown structure:

    ## <emoji> IMAGE 1 — <title>
    ```text
    SCENE LOCK: ...
    STAGE: ...
    DETAILS: ...
    NEGATIVE: ...
    ```

    ## <emoji> IMAGE 2 — <title>
    ...

    ## <emoji> VIDEO 1 — <title>
    ```text
    SCENE LOCK: ...
    STAGE: ...
    DETAILS: ...
    NEGATIVE: ...
    AUDIO: ...
    ```

Note: VIDEO blocks include an AUDIO field (5 fields total) while IMAGE blocks
have 4 fields. The parser doesn't enforce field-level structure — it just
extracts the full text-block contents and passes them through. Seedance reads
the AUDIO line from the VIDEO prompt text directly.

This parser extracts the text-block contents for each IMAGE and VIDEO heading,
indexed by the numeric id in the heading. It is intentionally strict about the
GPT's own mandated format — if a block is missing, we fail loud rather than
silently dropping prompts.

Why a deterministic parser instead of asking Claude to reformat into JSON:
the GPT's natural-language output is what it was tuned on; forcing it into
JSON changes the quality of what it generates. Let the GPT be the GPT, then
extract mechanically.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


# Matches a heading like:
#   ## 🌿 IMAGE 1 — Raw Backyard Before Excavation
#   ## 🎬 VIDEO 4 — Cinematic Hero Push-In
#   ##🧱IMAGE 3— Finished Hidden Bunker (Unstaged)   (tolerates missing spaces)
#
# Capture groups:
#   1: "IMAGE" or "VIDEO"
#   2: the number (1-9)
#
# We allow any characters between the `##` and the kind word so the GPT's
# emoji choice doesn't matter — em-dashes and en-dashes and hyphens in titles
# also all pass through the (.*) at the end.
_HEADING_RE = re.compile(
    r"^##\s*[^\n]*?\b(IMAGE|VIDEO)\s+(\d+)\b[^\n]*$",
    re.MULTILINE | re.IGNORECASE,
)

# Matches a fenced code block. The GPT's spec says ```text but we tolerate
# any language tag or no tag at all, because real GPT output sometimes drops
# the tag.
_FENCE_RE = re.compile(
    r"```(?:[a-zA-Z]+)?\s*\n(.*?)\n```",
    re.DOTALL,
)


class PromptParseError(RuntimeError):
    """Raised when the GPT output doesn't match the expected format."""


@dataclass
class ParsedPrompts:
    """Result of parsing a Restoration Timelapse GPT response.

    Attributes:
        images: dict mapping image number (1, 2, 3, 4) to prompt text.
        videos: dict mapping video number (1, 2, 3, 4) to prompt text.
                Note: video 4 may be absent if the system prompt was
                modified to omit it (protagonist-closure mode).
        raw: the full original response, useful for logging / manifests.
    """
    images: dict[int, str]
    videos: dict[int, str]
    raw: str

    def image(self, n: int) -> str:
        """Get IMAGE n prompt. Raises KeyError with a clear message if missing."""
        if n not in self.images:
            raise KeyError(
                f"IMAGE {n} was not present in the parsed GPT response. "
                f"Available image numbers: {sorted(self.images.keys())}"
            )
        return self.images[n]

    def video(self, n: int) -> str:
        """Get VIDEO n prompt. Raises KeyError with a clear message if missing."""
        if n not in self.videos:
            raise KeyError(
                f"VIDEO {n} was not present in the parsed GPT response. "
                f"Available video numbers: {sorted(self.videos.keys())}"
            )
        return self.videos[n]


def parse(
    response: str,
    *,
    expect_images: Optional[set[int]] = None,
    expect_videos: Optional[set[int]] = None,
) -> ParsedPrompts:
    """
    Parse a Restoration Timelapse GPT response into a ParsedPrompts dataclass.

    Args:
        response: the raw markdown text from the GPT call.
        expect_images: set of IMAGE numbers that must be present. Defaults to
            {1, 2, 3, 4}. Pass a smaller set if the system prompt was modified
            to produce fewer images.
        expect_videos: set of VIDEO numbers that must be present. Defaults to
            {1, 2, 3, 4}. Pass {1, 2, 3} for protagonist-closure mode.

    Raises:
        PromptParseError if any expected IMAGE or VIDEO block is missing, or
        if a heading has no corresponding ```text fence.

    Returns:
        ParsedPrompts with .images and .videos dicts indexed by number.
    """
    if expect_images is None:
        expect_images = {1, 2, 3, 4}
    if expect_videos is None:
        expect_videos = {1, 2, 3, 4}

    headings = list(_HEADING_RE.finditer(response))
    if not headings:
        raise PromptParseError(
            "Found no IMAGE or VIDEO headings in the GPT response. "
            "Expected '## <emoji> IMAGE 1 — ...' style headings. "
            f"First 500 chars of response: {response[:500]!r}"
        )

    images: dict[int, str] = {}
    videos: dict[int, str] = {}

    for i, heading_match in enumerate(headings):
        kind = heading_match.group(1).upper()
        number = int(heading_match.group(2))

        # Look for the next ```text fence after this heading, but BEFORE the
        # next heading (so we don't accidentally steal the next block's text).
        block_start = heading_match.end()
        block_end = (
            headings[i + 1].start()
            if i + 1 < len(headings)
            else len(response)
        )
        block_slice = response[block_start:block_end]

        fence_match = _FENCE_RE.search(block_slice)
        if not fence_match:
            raise PromptParseError(
                f"Heading '{heading_match.group(0).strip()}' has no ```text "
                f"fenced code block after it. Slice: {block_slice[:300]!r}"
            )

        prompt_text = fence_match.group(1).strip()
        if kind == "IMAGE":
            images[number] = prompt_text
        else:
            videos[number] = prompt_text

    # Validate expected blocks are present
    missing_images = expect_images - images.keys()
    if missing_images:
        raise PromptParseError(
            f"Expected IMAGE prompts {sorted(expect_images)} but missing "
            f"{sorted(missing_images)}. Found: {sorted(images.keys())}"
        )
    missing_videos = expect_videos - videos.keys()
    if missing_videos:
        raise PromptParseError(
            f"Expected VIDEO prompts {sorted(expect_videos)} but missing "
            f"{sorted(missing_videos)}. Found: {sorted(videos.keys())}"
        )

    log.info(
        "Parsed GPT response: IMAGE %s, VIDEO %s",
        sorted(images.keys()), sorted(videos.keys()),
    )
    return ParsedPrompts(images=images, videos=videos, raw=response)
