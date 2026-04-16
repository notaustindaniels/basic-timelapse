# WaveSpeed API — Quick Reference

This is a working reference for the two WaveSpeed models the skill uses. It is deliberately terse. When anything on WaveSpeed's side changes, update this file and `scripts/config.py` together.

## Auth

All requests use:
```
Authorization: Bearer ${WAVESPEED_API_KEY}
```

Get a key from https://wavespeed.ai after signing up. The key is a single string; put it in `~/.zshenv` or `.env` (see README).

## Base URL

```
https://api.wavespeed.ai/api/v3
```

## Async task pattern (both models use this)

Every generation is a two-step async flow:

1. **Submit** — `POST` to the model endpoint. Response contains `data.id` (the `requestId`).
2. **Poll** — `GET /predictions/{requestId}/result` until `data.status == "completed"` (success) or `"failed"` (error).

When status is `completed`, `data.outputs` is an array of URLs to the generated artifact(s). Download them with a plain HTTP GET — no auth needed on the output URLs.

Poll interval: 2s for images (they usually finish in 5–15s), 5s for videos (30–120s typical). Set a hard timeout of 5 min for images, 15 min for videos.

## Model 1 — Nano Banana 2, text-to-image

**Endpoint**: `POST /google/nano-banana-2/text-to-image`

**Request body**:
```json
{
  "prompt": "your full scene prompt here",
  "aspect_ratio": "9:16",
  "resolution": "2k",
  "output_format": "png",
  "enable_sync_mode": false,
  "enable_base64_output": false
}
```

Notes:
- `aspect_ratio` options: `1:1`, `2:3`, `3:2`, `3:4`, `4:3`, `4:5`, `5:4`, `9:16`, `16:9`, `21:9`. For viral social format, use `9:16`.
- `resolution`: `512px`, `1k`, `2k`, `4k`. Default to `2k` for our pipeline — gives crisp input to Seedance without burning credits on 4k.
- `output_format`: `png` or `jpeg`. PNG for zero compression.
- Do NOT set `enable_sync_mode: true` — it holds the connection open and breaks under our polling loop.

## Model 2 — Nano Banana 2, edit (for chained scene generation)

**Endpoint**: `POST /google/nano-banana-2/edit`

**Request body**:
```json
{
  "prompt": "your scene-2 prompt here, explicitly referencing continuity from scene 1",
  "images": ["https://url-to-previous-scene.png"],
  "resolution": "2k",
  "output_format": "png",
  "enable_sync_mode": false,
  "enable_base64_output": false
}
```

This is how we replicate the transcript's "click Reference" step. Scene 1 is generated with text-to-image. Scenes 2, 3, 4 are generated with `edit`, passing the previous scene as the single reference image. Nano Banana 2 supports up to 14 reference images but we only pass 1 for camera lock.

## Model 3 — Seedance 2.0, image-to-video

**Launched April 2026.** Leads the Artificial Analysis image-to-video leaderboard (Elo 1351, beating Veo 3, Sora 2, Runway Gen-4.5). Native audio, multimodal reference support (up to 4 reference images), director-level camera control via prompt text.

**Endpoint**: `POST /bytedance/seedance-2.0/image-to-video`

**Request body** (per WaveSpeed's April 2026 launch blog and quick-start example):
```json
{
  "image": "https://url-to-start-frame.png",
  "last_image": "https://url-to-end-frame.png",
  "prompt": "Locked-off static tripod. Workers remove patio stones and dig a rectangular pit.",
  "duration": 5,
  "resolution": "1080p"
}
```

Notes:
- `image` is the primary reference / start frame (required).
- `last_image` is the optional end frame for start/end-frame control — the whole reason the chained timelapse workflow works.
- `resolution`: `480p`, `720p`, or `1080p`. User requested 1080p.
- `duration`: `5`, `10`, or `15` seconds only (no arbitrary values — this is stricter than v1.x's 4–12 range).
- **`camera_fixed` is gone.** v2.0 expects camera language in the prompt itself. The client auto-prepends "Locked-off static tripod camera, no camera movement." when `camera_fixed=True` is passed programmatically.
- **`generate_audio` is gone.** v2.0 always generates native synchronized audio. If you don't want it in the final video, strip it at the ffmpeg stage (we do — `stitch_video.py` uses `-an`).
- **`seed` is gone from the REST schema.** WaveSpeed's web playground still has a seed slider but the REST endpoint does not document it. Don't rely on seed-based reproducibility in automated pipelines — treat each generation as non-deterministic.
- **`aspect_ratio` is not a parameter on i2v.** Aspect ratio is inherited from the input image. Generate your Nano Banana 2 images at the final target ratio (9:16 for vertical viral format) and Seedance will match.
- **Multi-reference (up to 4 images)** is a separate capability, accessed via a `reference_images` array field — useful for locking character consistency across a series of clips, not needed for the scene-chained workflow.

**Pricing** (as of April 2026): $0.60 for 5s@480p up to $5.40 for 15s@1080p on WaveSpeed. 1080p is 3x the 480p base rate, duration scales linearly, audio doubles cost.

### Alternate Seedance 2.0 endpoints

WaveSpeed exposes faster/cheaper variants. Verified against the live API on 2026-04-16:

| Endpoint | Status | Use case |
|---|---|---|
| `/bytedance/seedance-2.0/image-to-video` | **Verified** | **Default. Hero-quality finals.** |
| `/bytedance/seedance-2.0-fast/image-to-video` | **Verified** | Faster inference, near-equal quality |
| `/bytedance/seedance-2.0/image-to-video-turbo` | **Verified** | 720p/1080p at near-480p speed |
| `/bytedance/seedance-2.0-fast/image-to-video-fast` | **DEAD** (400 "model not found") | Do not use |

All live variants share the same parameter schema. Swap by changing `SEEDANCE_I2V_ENDPOINT` in `scripts/config.py`. Check https://wavespeed.ai/models for current availability — endpoints may be added or removed.

## Response shape (both models)

Submit response:
```json
{
  "code": 200,
  "message": "success",
  "data": {
    "id": "pred_abc123...",
    "status": "created",
    "model": "google/nano-banana-2/text-to-image",
    "outputs": [],
    "created_at": "2026-04-16T15:30:00Z"
  }
}
```

Poll response (when complete):
```json
{
  "code": 200,
  "message": "success",
  "data": {
    "id": "pred_abc123...",
    "status": "completed",
    "outputs": ["https://static.wavespeed.ai/out/abc123.png"],
    "has_nsfw_contents": [false],
    "created_at": "...",
    "executed_at": "...",
    "error": ""
  }
}
```

Poll response (on failure):
```json
{
  "code": 200,
  "message": "success",
  "data": {
    "id": "pred_abc123...",
    "status": "failed",
    "outputs": [],
    "error": "human-readable error string"
  }
}
```

Note the wrapper `code: 200, message: "success"` refers to the *HTTP call succeeding*, not the generation job. Always check `data.status` separately.

## Error codes worth handling

- HTTP `401` — bad or missing API key. Surface the error clearly; tell the user to check `WAVESPEED_API_KEY`.
- HTTP `429` — rate limited. Exponential backoff, start at 5s, max 60s, 5 retries.
- HTTP `5xx` — transient server error. Retry 3 times with 2s backoff.
- `data.status == "failed"` with `error: "content policy violation"` — Nano Banana 2 rejected the prompt. Bubble this back to the user along with the prompt that failed; don't retry blindly.
