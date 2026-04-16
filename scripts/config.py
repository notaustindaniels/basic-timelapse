"""
Config for the bunker-timelapse skill.

All API keys come from environment variables. The env vars can be set three ways,
in precedence order (first match wins):

1. Current shell environment (e.g. exported in ~/.zshenv)
2. A .env file in the skill's root directory (auto-loaded via python-dotenv)
3. Not set — config.py raises a clear error at import time

Model IDs and WaveSpeed endpoints live here as single-line constants so swapping
Seedance v1.5 -> v2.0 (when it ships) is a one-line change.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- Load .env if present -----------------------------------------------------

SKILL_ROOT = Path(__file__).resolve().parent.parent
_DOTENV_PATH = SKILL_ROOT / ".env"

if _DOTENV_PATH.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_DOTENV_PATH)
    except ImportError:
        # python-dotenv is optional — if the user set env vars in their shell,
        # they don't need it. Only warn if the .env file exists but we can't load it.
        print(
            f"[config] NOTE: {_DOTENV_PATH} exists but python-dotenv is not "
            f"installed. Install with: pip install python-dotenv"
        )


# --- Required env vars --------------------------------------------------------

def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Required environment variable {name} is not set.\n"
            f"See README.md for setup instructions. Either:\n"
            f"  1. Add `export {name}=...` to ~/.zshenv and `source ~/.zshenv`\n"
            f"  2. Create {_DOTENV_PATH} with {name}=... (see .env.example)"
        )
    return val


WAVESPEED_API_KEY = _require("WAVESPEED_API_KEY")

# Claude auth: the Agent SDK reads CLAUDE_CODE_OAUTH_TOKEN automatically
# when it's set. We don't `_require` it here because the SDK gives its own
# helpful error if missing, and some users may prefer ANTHROPIC_API_KEY.
CLAUDE_OAUTH_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

if not (CLAUDE_OAUTH_TOKEN or ANTHROPIC_API_KEY):
    raise RuntimeError(
        "No Claude credential found. Set either:\n"
        "  CLAUDE_CODE_OAUTH_TOKEN   (recommended — uses your subscription)\n"
        "  ANTHROPIC_API_KEY         (falls back to pay-per-token API)\n"
        "See README.md for `claude setup-token` instructions."
    )


# --- WaveSpeed endpoints ------------------------------------------------------

WAVESPEED_BASE_URL = "https://api.wavespeed.ai/api/v3"

# Nano Banana 2 (Gemini 3.1 Flash Image)
NB2_T2I_ENDPOINT = "/google/nano-banana-2/text-to-image"
NB2_EDIT_ENDPOINT = "/google/nano-banana-2/edit"

# Seedance image-to-video (v2.0, launched April 2026 on WaveSpeed).
# Confirmed to support 1080p output and last_image start/end-frame control.
# If you want faster/cheaper iteration, swap to one of:
#   /bytedance/seedance-2.0-fast/image-to-video      (verified — faster inference)
#   /bytedance/seedance-2.0/image-to-video-turbo     (verified — 720p/1080p turbo)
# NOTE: /bytedance/seedance-2.0-fast/image-to-video-fast is DEAD (400 "model not found").
# The standard v2.0 endpoint below gives the best hero-shot quality.
SEEDANCE_I2V_ENDPOINT = "/bytedance/seedance-2.0/image-to-video"

# Prediction result poll endpoint template
PREDICTION_RESULT_ENDPOINT = "/predictions/{request_id}/result"


# --- Generation defaults ------------------------------------------------------

# Image generation (Nano Banana 2)
DEFAULT_IMAGE_RESOLUTION = "2k"        # 512px | 1k | 2k | 4k
DEFAULT_ASPECT_RATIO = "9:16"          # vertical / viral-friendly; used by NB2 only
DEFAULT_IMAGE_FORMAT = "png"

# Video generation (Seedance 2.0)
DEFAULT_VIDEO_RESOLUTION = "1080p"     # 480p | 720p | 1080p
DEFAULT_CLIP_DURATION = 5              # v2.0 accepts ONLY 5, 10, or 15 seconds
DEFAULT_GENERATE_AUDIO = True          # v2.0 always generates audio; we keep it in the final cut
DEFAULT_CAMERA_FIXED = True            # v2.0 has no param — handled via prompt prefix

# Polling
IMAGE_POLL_INTERVAL_SEC = 2
IMAGE_POLL_TIMEOUT_SEC = 300           # 5 minutes
VIDEO_POLL_INTERVAL_SEC = 5
VIDEO_POLL_TIMEOUT_SEC = 900           # 15 minutes


# --- Claude model ------------------------------------------------------------

# The Agent SDK picks the model from CLI flags / config; we pass this via
# ClaudeAgentOptions in claude_client.py.
#
# Override with CLAUDE_MODEL env var if your SDK version doesn't accept the
# default (model availability differs between SDK versions and subscription
# tiers — e.g. Opus 4.7 may not be available via every OAuth token).
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-6")
