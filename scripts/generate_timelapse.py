#!/usr/bin/env python3
"""
generate_timelapse.py — End-to-end orchestrator for the bunker-timelapse skill.

Architecture (refactored):

  1. The custom-GPT system prompt lives in
     references/restoration_system_prompt.md verbatim. It is a behavior
     contract for a creative assistant, not a schema. We do not edit it at
     runtime except for one case: protagonist-closure mode, which writes a
     modified version to /tmp/.

  2. scripts/claude_client.py is a dumb transport — it sends (system, user,
     optional image) and returns raw markdown. It knows nothing about our
     workflow.

  3. scripts/prompt_parser.py extracts IMAGE 1-4 and VIDEO 1-N blocks from
     the markdown via regex. Deterministic, no LLM in the loop.

  4. This orchestrator calls the GPT TWICE per run:
       - Phase A: generate the above-ground transformation (intent + optional
         hero concept image attached).
       - Phase B: generate the underground/interior reveal (Phase A's
         Scene 4 attached as the reference image so the GPT anchors on the
         established geometry).

  5. For each phase, we generate 4 images via NB2 (t2i for scene 1, /edit
     chain for scenes 2-4), then 3 video clips via Seedance (scene N -> N+1).

  6. Closure mode determines how the final clip is made:
       - `cinematic`: use the GPT's VIDEO 4 block (from Phase B) as a single-
         frame push-in on Phase B Scene 4.
       - `protagonist`: inject a character into Phase B Scene 4 via NB2 /edit,
         then Seedance-animate Phase B Scene 4 -> character image.

  7. ffmpeg stitches all clips into one MP4. Seedance 2.0's native audio is
     preserved by default.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# When invoked as `python scripts/generate_timelapse.py`, scripts/ needs to
# be on sys.path so sibling imports work.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_client
import prompt_parser
import stitch_video
import wavespeed_client
from config import DEFAULT_CLIP_DURATION

log = logging.getLogger("orchestrator")

SKILL_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT_PATH = SKILL_ROOT / "references" / "restoration_system_prompt.md"


# ============================================================================
# Manifest structures
# ============================================================================

@dataclass
class ImageArtifact:
    scene_label: str            # e.g. "phase_a_scene_1"
    prompt: str
    reference_image_url: Optional[str]
    local_path: str
    remote_url: str


@dataclass
class VideoArtifact:
    clip_label: str             # e.g. "phase_a_clip_1"
    start_image_url: str
    end_image_url: Optional[str]  # None for push-in closure
    animation_prompt: str
    duration: int
    local_path: str
    remote_url: str


@dataclass
class RunManifest:
    started_at: float
    intent: str
    closure_mode: str
    protagonist_description: Optional[str] = None

    phase_a_raw_gpt_response: str = ""
    phase_a_images: list[ImageArtifact] = field(default_factory=list)
    phase_a_clips: list[VideoArtifact] = field(default_factory=list)

    phase_b_raw_gpt_response: str = ""
    phase_b_images: list[ImageArtifact] = field(default_factory=list)
    phase_b_clips: list[VideoArtifact] = field(default_factory=list)

    closure_image: Optional[ImageArtifact] = None
    closure_clip: Optional[VideoArtifact] = None

    final_video: Optional[str] = None
    finished_at: Optional[float] = None
    system_prompt_path_used: Optional[str] = None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2, default=str)


# ============================================================================
# System prompt preparation (protagonist-mode tweak)
# ============================================================================

def prepare_system_prompt(closure_mode: str, run_tmp_dir: Path) -> Path:
    """
    Return a path to the system prompt file to use for this run.

    For 'cinematic' mode the canonical file is used unchanged (the GPT
    produces all 4 VIDEO blocks including the cinematic push-in, and we use
    VIDEO 4 for the closure).

    For 'protagonist' mode we copy the canonical prompt to a temp file and
    strip/adjust the VIDEO 4 guidance, because our closure mechanism replaces
    what VIDEO 4 would have been. The canonical file is NEVER modified.
    """
    if closure_mode == "cinematic":
        log.info("Using canonical system prompt: %s", SYSTEM_PROMPT_PATH)
        return SYSTEM_PROMPT_PATH

    if closure_mode == "protagonist":
        original = SYSTEM_PROMPT_PATH.read_text()
        # Surgical edits for protagonist mode — catch ALL four occurrences
        # of "VIDEO 1–4" / "VIDEO 4" language. The canonical prompt mentions
        # these in:
        #   line 29:  "4 VIDEO prompts (VIDEO 1–4)"
        #   line 101: "VIDEO 1: ..." through "VIDEO 4: ..." (the Video Stages list)
        #   line 160: "then build IMAGE 1–3 + VIDEO 1–4 to match"
        #   line 185: "animate VIDEO 1–4 with a frame-to-video option"
        modified = (
            original
            .replace(
                "4 VIDEO prompts (VIDEO 1–4)",
                "3 VIDEO prompts (VIDEO 1–3)",
            )
            .replace(
                "* **VIDEO 4:** Cinematic reveal (zoom or dolly-in)\n",
                "",
            )
            .replace(
                "IMAGE 1–3 + VIDEO 1–4",
                "IMAGE 1–3 + VIDEO 1–3",
            )
            .replace(
                "animate VIDEO 1–4",
                "animate VIDEO 1–3",
            )
        )
        # Append an explicit note so the GPT doesn't forget and produce
        # VIDEO 4 anyway out of habit.
        modified += (
            "\n\n---\n\n## ⚠️ Protagonist-Closure Mode\n\n"
            "This run uses an alternate closure mechanism. "
            "DO NOT produce a VIDEO 4 block. "
            "Only produce VIDEO 1, VIDEO 2, and VIDEO 3.\n"
        )

        run_tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = run_tmp_dir / "system_prompt_protagonist.md"
        tmp_path.write_text(modified)
        log.info("Using protagonist-mode system prompt: %s", tmp_path)
        return tmp_path

    raise ValueError(f"Unknown closure_mode: {closure_mode!r}")


# ============================================================================
# Core workflow helpers
# ============================================================================

def _generate_phase_images(
    phase_label: str,
    image_prompts: dict[int, str],
    output_dir: Path,
    anchor_image_url: Optional[str] = None,
) -> list[ImageArtifact]:
    """
    Generate 4 scene images for one phase.

    - Scene 1:
        - If anchor_image_url is None (Phase A), call NB2 t2i.
        - If anchor_image_url is set (Phase B), call NB2 /edit with the
          anchor as the reference image (this is how Phase B inherits
          Phase A's geometry).
    - Scenes 2, 3, 4: always NB2 /edit with the previous scene as reference.
    """
    if set(image_prompts.keys()) != {1, 2, 3, 4}:
        raise ValueError(
            f"Expected IMAGE 1-4 (got {sorted(image_prompts.keys())})"
        )
    artifacts: list[ImageArtifact] = []
    prev_url: Optional[str] = anchor_image_url

    for scene_num in (1, 2, 3, 4):
        prompt = image_prompts[scene_num]
        label = f"{phase_label}_scene_{scene_num}"
        local_path = output_dir / "images" / f"{label}.png"

        log.info("=" * 60)
        log.info("Generating %s (ref=%s)", label, "yes" if prev_url else "no")

        path, url = wavespeed_client.generate_image(
            prompt=prompt,
            output_path=local_path,
            reference_image_url=prev_url,
        )
        artifacts.append(ImageArtifact(
            scene_label=label,
            prompt=prompt,
            reference_image_url=prev_url,
            local_path=str(path),
            remote_url=url,
        ))
        prev_url = url  # chain

    return artifacts


def _generate_phase_clips(
    phase_label: str,
    images: list[ImageArtifact],
    video_prompts: dict[int, str],
    output_dir: Path,
    clip_duration: int,
    video_resolution: Optional[str] = None,
) -> list[VideoArtifact]:
    """
    Generate 3 Seedance clips per phase:
      - clip 1: scene 1 -> scene 2, prompt = VIDEO 1
      - clip 2: scene 2 -> scene 3, prompt = VIDEO 2
      - clip 3: scene 3 -> scene 4, prompt = VIDEO 3

    VIDEO 4 from the GPT is NOT used here — that's the closure mechanism,
    handled separately.
    """
    if len(images) != 4:
        raise ValueError(f"Need exactly 4 images per phase, got {len(images)}")

    for n in (1, 2, 3):
        if n not in video_prompts:
            raise ValueError(f"Missing VIDEO {n} prompt")

    artifacts: list[VideoArtifact] = []
    resolution_kwargs = {"resolution": video_resolution} if video_resolution else {}

    for i in range(3):
        clip_num = i + 1
        start_img = images[i]
        end_img = images[i + 1]
        label = f"{phase_label}_clip_{clip_num}"
        local_path = output_dir / "clips" / f"{label}.mp4"
        anim_prompt = video_prompts[clip_num]

        log.info("=" * 60)
        log.info(
            "Generating %s: %s -> %s (%ds, res=%s)",
            label, start_img.scene_label, end_img.scene_label, clip_duration,
            video_resolution or "default",
        )

        path, url = wavespeed_client.generate_video(
            start_image_url=start_img.remote_url,
            end_image_url=end_img.remote_url,
            prompt=anim_prompt,
            output_path=local_path,
            duration=clip_duration,
            **resolution_kwargs,
        )
        artifacts.append(VideoArtifact(
            clip_label=label,
            start_image_url=start_img.remote_url,
            end_image_url=end_img.remote_url,
            animation_prompt=anim_prompt,
            duration=clip_duration,
            local_path=str(path),
            remote_url=url,
        ))

    return artifacts


# ============================================================================
# Closure mechanisms
# ============================================================================

def _closure_cinematic(
    phase_b_scene_4: ImageArtifact,
    video_4_prompt: str,
    output_dir: Path,
    clip_duration: int,
    video_resolution: Optional[str],
) -> VideoArtifact:
    """
    Cinematic hero push-in closure: single-frame Seedance call on Phase B
    scene 4 with VIDEO 4's prompt.
    """
    local_path = output_dir / "clips" / "closure_cinematic.mp4"
    log.info("=" * 60)
    log.info("CLOSURE (cinematic): push-in on %s", phase_b_scene_4.scene_label)

    resolution_kwargs = {"resolution": video_resolution} if video_resolution else {}
    path, url = wavespeed_client.generate_video_push_in(
        start_image_url=phase_b_scene_4.remote_url,
        prompt=video_4_prompt,
        output_path=local_path,
        duration=clip_duration,
        **resolution_kwargs,
    )
    return VideoArtifact(
        clip_label="closure_cinematic",
        start_image_url=phase_b_scene_4.remote_url,
        end_image_url=None,
        animation_prompt=video_4_prompt,
        duration=clip_duration,
        local_path=str(path),
        remote_url=url,
    )


def _closure_protagonist(
    phase_b_scene_4: ImageArtifact,
    protagonist_description: str,
    output_dir: Path,
    clip_duration: int,
    video_resolution: Optional[str],
) -> tuple[ImageArtifact, VideoArtifact]:
    """
    Protagonist closure per the transcript's narrator's method:

      1. NB2 /edit: take Phase B scene 4, insert the protagonist organically.
         This yields the "closing" still image.
      2. Seedance: animate Phase B scene 4 -> closing still, with a motion
         prompt like "the person enters the scene and sits/stands/etc".

    Returns (closing_image, closing_clip).
    """
    # --- Step 1: the closing image ---
    insert_prompt = (
        f"Organically integrate the following person into this scene in a "
        f"natural, relaxed final-moment pose (sitting, leaning, standing at "
        f"rest — whatever fits the space): {protagonist_description}. "
        f"Preserve the scene's camera angle, framing, lighting, and all "
        f"architectural details exactly. The person should feel like they "
        f"belong in the space, not inserted — use matching lighting, shadows, "
        f"and perspective."
    )
    closing_image_path = output_dir / "images" / "closure_protagonist.png"

    log.info("=" * 60)
    log.info("CLOSURE (protagonist): generating closing still image")
    path, url = wavespeed_client.generate_image(
        prompt=insert_prompt,
        output_path=closing_image_path,
        reference_image_url=phase_b_scene_4.remote_url,
    )
    closing_image = ImageArtifact(
        scene_label="closure_protagonist",
        prompt=insert_prompt,
        reference_image_url=phase_b_scene_4.remote_url,
        local_path=str(path),
        remote_url=url,
    )

    # --- Step 2: the closing clip ---
    motion_prompt = (
        f"The person ({protagonist_description}) organically enters the "
        f"established scene and settles into a relaxed final pose. "
        f"Locked-off static tripod camera, no camera movement. "
        f"Audio: ambient room tone and natural footsteps only — no music, "
        f"no dramatic stings."
    )
    clip_path = output_dir / "clips" / "closure_protagonist.mp4"

    log.info("CLOSURE (protagonist): animating scene_4 -> closing_image")
    resolution_kwargs = {"resolution": video_resolution} if video_resolution else {}
    clip_local, clip_remote = wavespeed_client.generate_video(
        start_image_url=phase_b_scene_4.remote_url,
        end_image_url=closing_image.remote_url,
        prompt=motion_prompt,
        output_path=clip_path,
        duration=clip_duration,
        **resolution_kwargs,
    )
    closing_clip = VideoArtifact(
        clip_label="closure_protagonist",
        start_image_url=phase_b_scene_4.remote_url,
        end_image_url=closing_image.remote_url,
        animation_prompt=motion_prompt,
        duration=clip_duration,
        local_path=str(clip_local),
        remote_url=clip_remote,
    )

    return closing_image, closing_clip


# ============================================================================
# Main pipeline
# ============================================================================

def _build_phase_a_user_message(
    intent: str,
    vibe: Optional[str],
    features: Optional[str],
    lighting: Optional[str],
) -> str:
    """
    Text turn for the Phase A GPT call. Mirrors how a human would prompt the
    custom GPT, filling in answers to the GPT's own mandatory questionnaire
    so the GPT doesn't pause to ask.
    """
    lines = [
        f"Subject: {intent}",
        "",
        "No image is uploaded. Generate Phase A: the above-ground transformation.",
        "",
        "Here are my answers to your questionnaire so you don't need to ask:",
    ]
    if vibe:
        lines.append(f"- Vibe: {vibe}")
    if features:
        lines.append(f"- Must-have features: {features}")
    if lighting:
        lines.append(f"- Lighting: {lighting}")
    lines.append("")
    lines.append(
        "Produce the full 4 IMAGE + 4 VIDEO output per your Prompt Formatting "
        "Rules. (Or 4 IMAGE + 3 VIDEO if this is a protagonist-closure run.)"
    )
    return "\n".join(lines)


def _build_phase_b_user_message(intent: str) -> str:
    """
    Text turn for the Phase B GPT call. The attached image is Phase A's
    Scene 4. We want the GPT to build the underground/interior reveal that
    sits beneath or inside it.
    """
    return (
        f"Subject: {intent}\n"
        f"\n"
        f"The attached image is the final above-ground shot from Phase A "
        f"(Scene 4). Now produce Phase B: the underground or interior reveal "
        f"that sits below or within this space. Per your Image Upload Rule, "
        f"treat the attached image as the anchor — but for Phase B, the "
        f"'final state' should be what's revealed INSIDE/BELOW, not the "
        f"above-ground view.\n"
        f"\n"
        f"Phase B's Scene 1 should feel like a one-cut continuation of the "
        f"attached image (camera has descended or moved inside, but the "
        f"lighting tone, architectural details, and geometry match). "
        f"Scenes 2-4 should show the construction of the underground space, "
        f"ending at a viral-ready staged reveal (Scene 4).\n"
        f"\n"
        f"Produce the full 4 IMAGE + 4 VIDEO output per your Prompt "
        f"Formatting Rules. (Or 4 IMAGE + 3 VIDEO if protagonist-closure.)"
    )


def run_pipeline(
    intent: str,
    output_dir: Path,
    clip_duration: int,
    closure_mode: str,
    protagonist_description: Optional[str],
    preserve_audio: bool,
    vibe: Optional[str],
    features: Optional[str],
    lighting: Optional[str],
    smoke: bool,
    mini: bool,
) -> RunManifest:
    """End-to-end pipeline. Returns the completed manifest."""
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = RunManifest(
        started_at=time.time(),
        intent=intent,
        closure_mode=closure_mode,
        protagonist_description=protagonist_description,
    )

    video_resolution = "480p" if (smoke or mini) else None
    if smoke:
        log.info("[smoke] Using 480p resolution for video generation")
    elif mini:
        log.info("[mini] Full pipeline at 480p resolution")

    # --- Prepare the system prompt (may be temp-file-modified) -------------
    run_tmp_dir = Path(tempfile.mkdtemp(prefix="bunker_timelapse_"))
    system_prompt_path = prepare_system_prompt(closure_mode, run_tmp_dir)
    system_prompt = claude_client.load_system_prompt(system_prompt_path)
    manifest.system_prompt_path_used = str(system_prompt_path)

    expected_videos = (
        {1, 2, 3, 4} if closure_mode == "cinematic" else {1, 2, 3}
    )

    # --- Phase A: call the GPT ---------------------------------------------
    log.info("=" * 70)
    log.info("PHASE A: calling the Restoration Timelapse GPT")
    phase_a_user = _build_phase_a_user_message(intent, vibe, features, lighting)
    phase_a_raw = claude_client.send_prompt(
        system_prompt=system_prompt,
        user_text=phase_a_user,
        image_path=None,
    )
    manifest.phase_a_raw_gpt_response = phase_a_raw

    # Save raw response to disk immediately so if downstream fails, we have it
    (output_dir / "phase_a_gpt_response.md").write_text(phase_a_raw)

    phase_a_parsed = prompt_parser.parse(
        phase_a_raw,
        expect_images={1, 2, 3, 4},
        expect_videos=expected_videos,
    )

    # --- Phase A: generate images + clips ----------------------------------
    if smoke:
        log.info("[smoke] Truncating Phase A to 2 scenes + 1 clip")
        # Build a 2-image dict for smoke
        smoke_images = {1: phase_a_parsed.images[1], 2: phase_a_parsed.images[2]}
        # Fake out _generate_phase_images' validation by running it manually
        phase_a_img_artifacts: list[ImageArtifact] = []
        prev_url: Optional[str] = None
        for scene_num in (1, 2):
            prompt = smoke_images[scene_num]
            label = f"phase_a_scene_{scene_num}"
            local_path = output_dir / "images" / f"{label}.png"
            path, url = wavespeed_client.generate_image(
                prompt=prompt,
                output_path=local_path,
                reference_image_url=prev_url,
            )
            phase_a_img_artifacts.append(ImageArtifact(
                scene_label=label,
                prompt=prompt,
                reference_image_url=prev_url,
                local_path=str(path),
                remote_url=url,
            ))
            prev_url = url
        manifest.phase_a_images = phase_a_img_artifacts

        # One clip: scene 1 -> scene 2 with VIDEO 1 prompt
        clip_local_path = output_dir / "clips" / "phase_a_clip_1.mp4"
        res_kwargs = {"resolution": video_resolution} if video_resolution else {}
        clip_path, clip_url = wavespeed_client.generate_video(
            start_image_url=phase_a_img_artifacts[0].remote_url,
            end_image_url=phase_a_img_artifacts[1].remote_url,
            prompt=phase_a_parsed.videos[1],
            output_path=clip_local_path,
            duration=clip_duration,
            **res_kwargs,
        )
        manifest.phase_a_clips = [VideoArtifact(
            clip_label="phase_a_clip_1",
            start_image_url=phase_a_img_artifacts[0].remote_url,
            end_image_url=phase_a_img_artifacts[1].remote_url,
            animation_prompt=phase_a_parsed.videos[1],
            duration=clip_duration,
            local_path=str(clip_path),
            remote_url=clip_url,
        )]
    else:
        log.info("PHASE A: generating 4 scene images")
        manifest.phase_a_images = _generate_phase_images(
            phase_label="phase_a",
            image_prompts=phase_a_parsed.images,
            output_dir=output_dir,
        )
        log.info("PHASE A: generating 3 clips")
        manifest.phase_a_clips = _generate_phase_clips(
            phase_label="phase_a",
            images=manifest.phase_a_images,
            video_prompts=phase_a_parsed.videos,
            output_dir=output_dir,
            clip_duration=clip_duration,
            video_resolution=video_resolution,
        )

    # --- Smoke mode bails out here -----------------------------------------
    if smoke:
        log.info("[smoke] Skipping Phase B and closure")
        final_path = output_dir / "final.mp4"
        if len(manifest.phase_a_clips) == 1:
            import shutil
            shutil.copy2(manifest.phase_a_clips[0].local_path, final_path)
        manifest.final_video = str(final_path)
        manifest.finished_at = time.time()
        manifest.save(output_dir / "manifest.json")
        return manifest

    # --- Phase B: call the GPT with Phase A Scene 4 as reference ----------
    log.info("=" * 70)
    log.info("PHASE B: calling the GPT with Phase A Scene 4 attached")
    phase_a_scene_4 = manifest.phase_a_images[-1]
    phase_b_user = _build_phase_b_user_message(intent)
    phase_b_raw = claude_client.send_prompt(
        system_prompt=system_prompt,
        user_text=phase_b_user,
        image_path=Path(phase_a_scene_4.local_path),
    )
    manifest.phase_b_raw_gpt_response = phase_b_raw
    (output_dir / "phase_b_gpt_response.md").write_text(phase_b_raw)

    phase_b_parsed = prompt_parser.parse(
        phase_b_raw,
        expect_images={1, 2, 3, 4},
        expect_videos=expected_videos,
    )

    # --- Phase B: generate images + clips ---------------------------------
    log.info("PHASE B: generating 4 scene images anchored on Phase A Scene 4")
    manifest.phase_b_images = _generate_phase_images(
        phase_label="phase_b",
        image_prompts=phase_b_parsed.images,
        output_dir=output_dir,
        anchor_image_url=phase_a_scene_4.remote_url,
    )
    log.info("PHASE B: generating 3 clips")
    manifest.phase_b_clips = _generate_phase_clips(
        phase_label="phase_b",
        images=manifest.phase_b_images,
        video_prompts=phase_b_parsed.videos,
        output_dir=output_dir,
        clip_duration=clip_duration,
        video_resolution=video_resolution,
    )

    # --- Closure ----------------------------------------------------------
    phase_b_scene_4 = manifest.phase_b_images[-1]
    if closure_mode == "cinematic":
        manifest.closure_clip = _closure_cinematic(
            phase_b_scene_4=phase_b_scene_4,
            video_4_prompt=phase_b_parsed.videos[4],
            output_dir=output_dir,
            clip_duration=clip_duration,
            video_resolution=video_resolution,
        )
    else:  # protagonist
        assert protagonist_description is not None, (
            "protagonist closure requires --protagonist-description"
        )
        closing_image, closing_clip = _closure_protagonist(
            phase_b_scene_4=phase_b_scene_4,
            protagonist_description=protagonist_description,
            output_dir=output_dir,
            clip_duration=clip_duration,
            video_resolution=video_resolution,
        )
        manifest.closure_image = closing_image
        manifest.closure_clip = closing_clip

    # --- Stitch ------------------------------------------------------------
    log.info("=" * 70)
    log.info("Stitching final video")
    all_clips = [Path(c.local_path) for c in manifest.phase_a_clips]
    all_clips += [Path(c.local_path) for c in manifest.phase_b_clips]
    if manifest.closure_clip:
        all_clips.append(Path(manifest.closure_clip.local_path))

    final_path = output_dir / "final.mp4"
    stitch_video.stitch_clips(all_clips, final_path, preserve_audio=preserve_audio)
    manifest.final_video = str(final_path)

    manifest.finished_at = time.time()
    manifest.save(output_dir / "manifest.json")
    return manifest


# ============================================================================
# CLI
# ============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a viral restoration/bunker timelapse video.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--intent", required=True,
        help="Subject of the transformation. Example: 'hidden survival "
             "bunker under a suburban backyard patio'.",
    )
    parser.add_argument(
        "--output-dir", required=True, type=Path,
        help="Directory to write all artifacts into.",
    )
    parser.add_argument(
        "--closure", choices=["cinematic", "protagonist"], default="cinematic",
        help="Closure mode for the final clip. 'cinematic' = GPT's VIDEO 4 "
             "push-in on Phase B Scene 4. 'protagonist' = the transcript's "
             "method: insert a character into Phase B Scene 4 and animate "
             "scene_4 -> character.",
    )
    parser.add_argument(
        "--protagonist-description", default=None,
        help="Required when --closure=protagonist. Natural-language "
             "description of the character to insert. Example: 'a man in his "
             "40s, jeans and a flannel shirt'. Pass 'auto' to let the image "
             "model invent one fitting the scene.",
    )
    parser.add_argument(
        "--vibe", default=None,
        help="Answer to the GPT's vibe question (modern / rustic / industrial / etc).",
    )
    parser.add_argument(
        "--features", default=None,
        help="Must-have features (e.g. 'hidden hatch with warm interior glow').",
    )
    parser.add_argument(
        "--lighting", default=None,
        help="Lighting condition (overcast / golden hour / dusk / etc).",
    )
    parser.add_argument(
        "--clip-duration", type=int, default=DEFAULT_CLIP_DURATION,
        choices=[5, 10, 15],
        help=f"Seconds per clip (Seedance 2.0 accepts 5, 10, or 15 — default {DEFAULT_CLIP_DURATION}).",
    )
    parser.add_argument(
        "--no-audio", action="store_true",
        help="Strip Seedance's native audio from the final MP4. Audio is "
             "preserved by default because the ambient sound adds punch.",
    )
    parser.add_argument(
        "--smoke", action="store_true",
        help="Smoke test: 2 Phase A scenes + 1 clip at 480p. Skips Phase B "
             "and closure. ~$0.70.",
    )
    parser.add_argument(
        "--mini", action="store_true",
        help="Mini test: full pipeline at 480p. ~$5-6 (closure adds cost).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging.",
    )

    args = parser.parse_args()

    if args.smoke and args.mini:
        parser.error("--smoke and --mini are mutually exclusive")

    if args.closure == "protagonist" and not args.protagonist_description:
        parser.error(
            "--closure=protagonist requires --protagonist-description. "
            "Pass a natural-language description of the character, or "
            "'auto' to let the image model invent one."
        )

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)

    protagonist_desc = args.protagonist_description
    if protagonist_desc == "auto":
        protagonist_desc = (
            "a person appropriate to the scene — their age, clothing, and "
            "demeanor should match the space's intended owner/occupant"
        )

    manifest = run_pipeline(
        intent=args.intent,
        output_dir=args.output_dir,
        clip_duration=args.clip_duration,
        closure_mode=args.closure,
        protagonist_description=protagonist_desc,
        preserve_audio=not args.no_audio,
        vibe=args.vibe,
        features=args.features,
        lighting=args.lighting,
        smoke=args.smoke,
        mini=args.mini,
    )

    print("\n" + "=" * 70)
    print(f"DONE. Final video: {manifest.final_video}")
    print(f"Manifest: {args.output_dir / 'manifest.json'}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
