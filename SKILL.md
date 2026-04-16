---
name: bunker-timelapse
description: Programmatically generate viral "hidden bunker" / restoration timelapse videos end-to-end using Nano Banana 2 (images) and Seedance (image-to-video), orchestrated with Claude via OAuth. Use this skill whenever the user asks to "make a bunker timelapse", "generate a restoration timelapse", "build an underground bunker video", "create a backyard transformation video", or anything that matches the viral two-phase (above-ground + underground) construction-reveal format. Also use when the user references the Restoration Timelapse GPT workflow, viral transformation videos, before/after construction sequences, or asks to automate a Higgsfield/Kling-style pipeline. This skill replaces the entire manual web-UI workflow (Pinterest search → ChatGPT prompt generation → Higgsfield image gen → Kling i2v) with a single programmatic pipeline.
---

# Bunker Timelapse Skill

Generate a full viral-style restoration timelapse video end-to-end from a short user intent, with no web UI clicking. The skill orchestrates three models:

- **Claude (via OAuth subscription)** — prompt engineering: turns user intent into a hero concept + 4 stage-locked scene prompts + 3 animation prompts per phase.
- **Nano Banana 2** (WaveSpeed) — generates all reference and scene images.
- **Seedance 2.0 image-to-video** (WaveSpeed) — animates consecutive scene pairs using start-frame + end-frame control at 1080p.
- **ffmpeg** — stitches the 6 clips into one final timelapse.

## When to trigger this skill

Run this skill when the user says anything like:
- "make me a bunker timelapse"
- "build me a viral restoration timelapse"
- "I want that backyard-to-bunker video"
- "can you automate the Restoration Timelapse GPT workflow"
- "generate a transformation video of X being built"

If the user mentions a specific subject (e.g. "a bunker in my backyard", "a cabin in the woods being restored", "an abandoned gas station rebuilt"), use that as the intent. If they say "make me a bunker timelapse" with no subject, ask them briefly for vibe + must-have features + lighting (see Step 0 below).

## High-level workflow

The viral format the user wants follows the transcript they provided. Two phases, chained:

```
Phase A (above-ground):  scene1 → scene2 → scene3 → scene4   (4 images, 3 clips)
Phase B (underground):   scene1 → scene2 → scene3 → scene4   (4 images, 3 clips)

Total: 8 images, 6 clips, 1 final stitched MP4.
```

Each clip uses scene N as the first frame and scene N+1 as the last frame, which is what keeps the geometry locked across the timelapse. This is critical and is easy to get wrong — read `references/workflow_diagram.md` before editing the orchestrator.

## Step 0 — Gather intent (if not already given)

If the user already gave a concrete subject + vibe, skip this step. Otherwise ask them three short questions:

1. **Vibe**: modern / rustic / industrial / luxury / post-apocalyptic / something else
2. **Must-have features**: e.g. "open hatch with glowing interior", "hydraulic door", "spiral staircase visible"
3. **Lighting**: overcast daytime / golden hour / dusk / night with interior glow

Don't over-ask. Three questions is the cap. If the user gives a one-line answer like "a survival bunker under a Colorado cabin, golden hour, with a hidden hatch", that is enough.

Also ask which mode they want (see `--mode` flag below):
- `hero` — generate one hero concept image first, then reverse-engineer the 4 stages from it
- `direct` — skip the hero concept, generate the 4 stages directly from intent
- `both` — do both in parallel so the user can compare (default)

## Step 1 — Run the orchestrator

All the work happens in `scripts/generate_timelapse.py`. From the skill's root directory:

```bash
python scripts/generate_timelapse.py \
  --intent "hidden survival bunker under a suburban backyard patio, overcast daylight, hatch opens to reveal warm interior glow" \
  --mode both \
  --output-dir ./runs/$(date +%Y%m%d_%H%M%S)
```

The orchestrator does the following, in order:
1. Loads API keys from environment (see `README.md` for setup).
2. Calls Claude via the OAuth-authenticated Agent SDK to generate prompts. The Claude call uses the system prompt stored in `references/restoration_system_prompt.md` — **do not inline it in code**, always read it from the reference file so it stays editable.
3. If `--mode` is `hero` or `both`: calls Nano Banana 2 to generate a hero concept image, then sends that image back to Claude to reverse-engineer the 4 Phase A scene prompts.
4. If `--mode` is `direct` or `both`: calls Claude to generate 4 Phase A scene prompts directly from the intent, no hero.
5. Generates the 4 Phase A images with Nano Banana 2 in sequence. Each call includes the prior image as a reference (via the `edit` endpoint) so the camera stays locked — this is how the transcript's "reference button" step is replicated programmatically.
6. Generates 3 Phase A video clips with Seedance image-to-video, each using `image` (start) + `last_image` (end) + the matching animation prompt from Claude.
7. Repeats 5–6 for Phase B (underground). Phase B's hero image is conditioned on the final Phase A image so the two phases narratively connect.
8. Downloads all clips, concatenates them with ffmpeg.
9. Writes a run manifest (`manifest.json`) documenting every prompt, every model call, every output file, and the full provenance.

## Step 2 — Present the result

When the orchestrator finishes it prints the path to the final MP4 (or to both MP4s if `--mode both`). Confirm the run succeeded, hand the user the final file path(s), and offer to iterate on any stage — for example if scene 3 looks off, they can re-run just that scene and the downstream clip with `--start-at phase_a_scene_3` (see the script's `--help` for partial-run flags).

## Reference files in this skill

- `references/restoration_system_prompt.md` — the exact system prompt for Claude, lifted from the user's "Restoration Timelapse" GPT instructions. The orchestrator reads this at runtime.
- `references/wavespeed_api.md` — quick reference for both WaveSpeed endpoints used: request shape, poll pattern, known quirks (e.g. the difference between `seedance-v1.5-pro` and the hypothetical `seedance-2.0` endpoint).
- `references/workflow_diagram.md` — the scene-chaining pattern as a diagram, with the exact start-frame/end-frame mapping for the 6 clips. Read this before editing `generate_timelapse.py`.

## Scripts in this skill

- `scripts/generate_timelapse.py` — top-level CLI orchestrator. Run this.
- `scripts/claude_client.py` — wraps the Claude Agent SDK with the OAuth token. One function, `generate_prompts(intent, mode, hero_image_path=None) -> dict`.
- `scripts/wavespeed_client.py` — two functions: `generate_image(...)` and `generate_video(...)`. Each submits a task, polls `/predictions/{id}/result` until `completed` or `failed`, and downloads the artifact. Both handle timeouts and retries.
- `scripts/stitch_video.py` — ffmpeg concat invocation. Takes a list of MP4 paths, emits one stitched MP4.
- `scripts/config.py` — loads env vars, exposes model IDs as constants so they're easy to swap between Seedance 2.0 variants (standard / fast / turbo).

## Important: Seedance model version

This skill uses `bytedance/seedance-2.0/image-to-video` by default — WaveSpeed's top-tier image-to-video endpoint as of April 2026 (the leaderboard-leading ByteDance model). It supports 1080p output and the `last_image` parameter for start-end frame control, both of which the chained scene workflow depends on.

`scripts/config.py` exposes `SEEDANCE_I2V_ENDPOINT` as a single constant. If you want cheaper/faster iteration, swap it to `seedance-2.0-fast/image-to-video` (-17% cost) or `seedance-2.0-fast/image-to-video-fast` (-33% cost). All v2.0 variants share the same request schema.

Note: Seedance 2.0 dropped three v1.x parameters:
- `camera_fixed` — camera lock now goes in the prompt text ("locked-off tripod"). The client handles this automatically when `camera_fixed=True` is passed.
- `generate_audio` — v2.0 always generates native audio. This skill preserves it in the final cut by default (the ambient sound enhances the timelapse reveal). Pass `--no-audio` to strip it, or edit `scripts/stitch_video.py`'s audio encoding flags to customize.
- `seed` — no documented reproducibility knob in v2.0. Treat each clip generation as non-deterministic.

Duration is also stricter: v2.0 accepts only 5, 10, or 15 seconds (not the 4–12 continuous range v1.x had).

## Common failure modes

- **OAuth token expired**: the Claude Agent SDK's OAuth token expires roughly every 8 hours of the subscription session, but long-lived tokens from `claude setup-token` last ~1 year. If `claude_client.py` raises an auth error, direct the user to regenerate with `claude setup-token` (see README).
- **WaveSpeed 429 rate limit**: the client has exponential backoff built in, but under heavy use the user may still hit the limit. The error message will tell them to wait.
- **Scene drift**: if the Nano Banana 2 outputs drift off the reference composition, the first remedy is to tighten the "SCENE LOCK" block in the system prompt. Edit `references/restoration_system_prompt.md` — the orchestrator reads it fresh on every run.

## Design invariants (do not break these)

1. The Claude system prompt lives in `references/restoration_system_prompt.md` — never inlined.
2. Each Phase A image after scene 1 is generated with the previous scene as an edit reference, not as an independent t2i call. This is the "Reference button" in the transcript, replicated via the `nano-banana-2/edit` endpoint.
3. Each video clip uses `image` = scene N and `last_image` = scene N+1. Never scene 1 as start for every clip.
4. The stitched output is always presented along with the individual artifacts so the user can regenerate partial scenes.
