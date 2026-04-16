"""
WaveSpeed API client.

Two public functions: generate_image() and generate_video().
Both follow the same async pattern:
    1. POST to the model endpoint, get back a request_id
    2. Poll /predictions/{id}/result until status is completed or failed
    3. Download the artifact URL to disk

Both handle 429 rate limits with exponential backoff, and surface failed
generations with the WaveSpeed error string intact so the caller can decide
whether to retry or bubble up.
"""
from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Any, Optional

import requests

from config import (
    DEFAULT_ASPECT_RATIO,
    DEFAULT_CAMERA_FIXED,
    DEFAULT_CLIP_DURATION,
    DEFAULT_GENERATE_AUDIO,
    DEFAULT_IMAGE_FORMAT,
    DEFAULT_IMAGE_RESOLUTION,
    DEFAULT_VIDEO_RESOLUTION,
    IMAGE_POLL_INTERVAL_SEC,
    IMAGE_POLL_TIMEOUT_SEC,
    NB2_EDIT_ENDPOINT,
    NB2_T2I_ENDPOINT,
    PREDICTION_RESULT_ENDPOINT,
    SEEDANCE_I2V_ENDPOINT,
    VIDEO_POLL_INTERVAL_SEC,
    VIDEO_POLL_TIMEOUT_SEC,
    WAVESPEED_API_KEY,
    WAVESPEED_BASE_URL,
)

log = logging.getLogger(__name__)


class WaveSpeedError(RuntimeError):
    """Raised when WaveSpeed returns a generation failure we can't recover from."""


# --- Low-level HTTP helpers ---------------------------------------------------

def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {WAVESPEED_API_KEY}",
        "Content-Type": "application/json",
    }


def _post_with_retry(url: str, json: dict, max_retries: int = 5) -> dict:
    """POST with exponential backoff on 429/5xx. Returns parsed JSON body."""
    for attempt in range(max_retries):
        resp = requests.post(url, headers=_headers(), json=json, timeout=60)

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 401:
            raise WaveSpeedError(
                "WaveSpeed returned 401 Unauthorized. Your WAVESPEED_API_KEY is "
                "missing, invalid, or expired. Check ~/.zshenv or .env."
            )

        if resp.status_code == 429 or resp.status_code >= 500:
            # transient — backoff
            wait = min(5 * (2 ** attempt), 60) + random.uniform(0, 1)
            log.warning(
                "WaveSpeed %d on POST %s — retry %d/%d in %.1fs",
                resp.status_code, url, attempt + 1, max_retries, wait,
            )
            time.sleep(wait)
            continue

        # other 4xx — non-retryable
        raise WaveSpeedError(
            f"WaveSpeed POST {url} failed with {resp.status_code}: {resp.text[:500]}"
        )

    raise WaveSpeedError(
        f"WaveSpeed POST {url} exhausted {max_retries} retries (rate limit or 5xx)."
    )


def _poll_until_done(
    request_id: str,
    poll_interval: float,
    timeout_sec: float,
) -> list[str]:
    """Poll the prediction endpoint until status is completed. Returns the outputs URL list."""
    url = WAVESPEED_BASE_URL + PREDICTION_RESULT_ENDPOINT.format(request_id=request_id)
    start = time.monotonic()
    last_status = None

    while True:
        elapsed = time.monotonic() - start
        if elapsed > timeout_sec:
            raise WaveSpeedError(
                f"WaveSpeed prediction {request_id} timed out after {timeout_sec:.0f}s "
                f"(last status: {last_status})"
            )

        try:
            resp = requests.get(url, headers=_headers(), timeout=30)
        except requests.RequestException as e:
            log.warning("Poll error on %s: %s — continuing", request_id, e)
            time.sleep(poll_interval)
            continue

        if resp.status_code != 200:
            log.warning(
                "Poll %s returned HTTP %d: %s — continuing",
                request_id, resp.status_code, resp.text[:200],
            )
            time.sleep(poll_interval)
            continue

        body = resp.json()
        data = body.get("data", {})
        status = data.get("status")
        last_status = status

        if status == "completed":
            outputs = data.get("outputs", [])
            if not outputs:
                raise WaveSpeedError(
                    f"Prediction {request_id} completed but has no outputs. Body: {body}"
                )
            return outputs

        if status == "failed":
            err = data.get("error", "(no error message)")
            raise WaveSpeedError(f"Prediction {request_id} failed: {err}")

        # statuses: created, processing — keep polling
        log.debug("Prediction %s status=%s elapsed=%.1fs", request_id, status, elapsed)
        time.sleep(poll_interval)


def _download(url: str, dest: Path) -> Path:
    """Stream-download a URL to disk. Returns the destination path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)
    return dest


# --- Public: Nano Banana 2 image generation -----------------------------------

def generate_image(
    prompt: str,
    output_path: Path,
    reference_image_url: Optional[str] = None,
    *,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    resolution: str = DEFAULT_IMAGE_RESOLUTION,
    output_format: str = DEFAULT_IMAGE_FORMAT,
) -> tuple[Path, str]:
    """
    Generate a single image with Nano Banana 2.

    If `reference_image_url` is provided, uses the /edit endpoint for continuity.
    Otherwise uses /text-to-image for a fresh generation.

    Returns (local_path, remote_url).
    """
    if reference_image_url is None:
        endpoint = NB2_T2I_ENDPOINT
        payload = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "output_format": output_format,
            "enable_sync_mode": False,
            "enable_base64_output": False,
        }
    else:
        endpoint = NB2_EDIT_ENDPOINT
        payload = {
            "prompt": prompt,
            "images": [reference_image_url],
            "resolution": resolution,
            "output_format": output_format,
            "enable_sync_mode": False,
            "enable_base64_output": False,
        }

    log.info("Submitting image gen: endpoint=%s ref=%s", endpoint, bool(reference_image_url))
    submit = _post_with_retry(WAVESPEED_BASE_URL + endpoint, payload)
    request_id = submit["data"]["id"]
    log.info("Submitted request %s, polling for completion", request_id)

    output_urls = _poll_until_done(
        request_id,
        poll_interval=IMAGE_POLL_INTERVAL_SEC,
        timeout_sec=IMAGE_POLL_TIMEOUT_SEC,
    )
    remote_url = output_urls[0]
    log.info("Image ready: %s — downloading", remote_url)
    _download(remote_url, output_path)
    return output_path, remote_url


# --- Public: Seedance image-to-video ------------------------------------------

def generate_video(
    start_image_url: str,
    end_image_url: str,
    prompt: str,
    output_path: Path,
    *,
    duration: int = DEFAULT_CLIP_DURATION,
    resolution: str = DEFAULT_VIDEO_RESOLUTION,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    generate_audio: bool = DEFAULT_GENERATE_AUDIO,  # accepted for API compat; ignored in v2.0
    camera_fixed: bool = DEFAULT_CAMERA_FIXED,
    seed: int = -1,  # accepted for API compat; ignored in v2.0
) -> tuple[Path, str]:
    """
    Generate a video clip animating from start frame to end frame with Seedance 2.0.

    The prompt should describe the MOTION between the two frames, not the framing
    (framing is locked by the start/end images).

    IMPORTANT — Seedance 2.0 schema differences from v1.x:
      * `camera_fixed` was removed. Camera stillness is now prompt-driven — we
        prepend "locked-off tripod, static camera" to the prompt when
        camera_fixed=True.
      * `generate_audio` was removed. Audio is always generated natively by v2.0.
        We strip audio at the ffmpeg stitching stage via `-an` if you don't
        want it in the final cut.
      * `seed` was removed. v2.0 does not expose a reproducibility knob through
        the API schema (a `seed` field appears in WaveSpeed's web playground
        but is not documented in the REST schema; we keep the parameter in the
        function signature for API compat but do not send it to the endpoint).
      * Aspect ratio is inherited from the input image and is no longer a
        separate field on the i2v endpoint — we do not send `aspect_ratio`.

    Returns (local_path, remote_url).
    """
    # v2.0 dropped `camera_fixed` as an explicit parameter. If the caller asked
    # for a locked camera, inject it into the prompt text.
    if camera_fixed and "locked" not in prompt.lower() and "static" not in prompt.lower():
        prompt = f"Locked-off static tripod camera, no camera movement. {prompt}"

    # Note: if `generate_audio=False` and audio still lands in the output, it
    # will be stripped by ffmpeg's `-an` flag during stitching. Seedance 2.0
    # does not expose a way to skip audio generation at the model level.
    if not generate_audio:
        log.debug(
            "generate_audio=False requested but v2.0 always generates audio; "
            "it will be stripped during ffmpeg stitching."
        )

    payload: dict[str, Any] = {
        "image": start_image_url,
        "last_image": end_image_url,
        "prompt": prompt,
        "duration": duration,
        "resolution": resolution,
    }

    log.info(
        "Submitting video gen: duration=%ds res=%s camera_fixed=%s (via prompt)",
        duration, resolution, camera_fixed,
    )
    submit = _post_with_retry(WAVESPEED_BASE_URL + SEEDANCE_I2V_ENDPOINT, payload)
    request_id = submit["data"]["id"]
    log.info("Submitted video request %s, polling", request_id)

    output_urls = _poll_until_done(
        request_id,
        poll_interval=VIDEO_POLL_INTERVAL_SEC,
        timeout_sec=VIDEO_POLL_TIMEOUT_SEC,
    )
    remote_url = output_urls[0]
    log.info("Video ready: %s — downloading", remote_url)
    _download(remote_url, output_path)
    return output_path, remote_url
