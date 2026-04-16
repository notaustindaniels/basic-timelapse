#!/usr/bin/env python3
"""
generate_timelapse.py — End-to-end orchestrator for the bunker-timelapse skill.

Workflow:
    1. Load API keys + system prompt.
    2. Call Claude to generate prompts (one or both modes).
    3. Generate 4 images for Phase A via Nano Banana 2 (t2i for scene 1, edit
       chain for scenes 2-4).
    4. Generate 3 video clips for Phase A via Seedance (scene N -> scene N+1).
    5. Repeat 3-4 for Phase B, anchored on Phase A scene 4.
    6. Stitch the 6 clips with ffmpeg.
    7. Write a manifest.json documenting everything.

Example:
    python scripts/generate_timelapse.py \
        --intent "hidden survival bunker under a suburban backyard patio, overcast daylight" \
        --mode both \
        --output-dir ./runs/$(date +%Y%m%d_%H%M%S)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# When invoked as `python scripts/generate_timelapse.py`, the scripts/ dir
# needs to be on sys.path so sibling imports work.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_client
import stitch_video
import wavespeed_client
from config import DEFAULT_CLIP_DURATION

log = logging.getLogger("orchestrator")


# --- Manifest structures ------------------------------------------------------

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
    end_image_url: str
    animation_prompt: str
    duration: int
    local_path: str
    remote_url: str


@dataclass
class RunManifest:
    started_at: float
    intent: str
    mode: str
    claude_response: dict = field(default_factory=dict)
    hero_image: Optional[ImageArtifact] = None
    phase_a_images: list[ImageArtifact] = field(default_factory=list)
    phase_a_clips: list[VideoArtifact] = field(default_factory=list)
    phase_b_images: list[ImageArtifact] = field(default_factory=list)
    phase_b_clips: list[VideoArtifact] = field(default_factory=list)
    final_video: Optional[str] = None
    finished_at: Optional[float] = None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2, default=str)


# --- Core workflow ------------------------------------------------------------

def _generate_phase_images(
    phase_label: str,
    scene_prompts: list[str],
    output_dir: Path,
    anchor_image_url: Optional[str] = None,
) -> list[ImageArtifact]:
    """
    Generate the 4 scene images for a phase.

    Scene 1:
        - If `anchor_image_url` is None (Phase A standalone), call t2i
        - If `anchor_image_url` is set (Phase B continuation), call /edit with
          the anchor (Phase A scene 4) as reference so the geometry carries over.
    Scenes 2-4: always /edit with the previous scene as reference.
    """
    assert 2 <= len(scene_prompts) <= 4, (
        f"Expected 2-4 scene prompts (2 in smoke mode, 4 in normal mode), "
        f"got {len(scene_prompts)}"
    )
    artifacts: list[ImageArtifact] = []
    prev_url: Optional[str] = anchor_image_url

    for i, prompt in enumerate(scene_prompts):
        scene_num = i + 1
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
        prev_url = url  # next scene chains off this one

    return artifacts


def _generate_phase_clips(
    phase_label: str,
    images: list[ImageArtifact],
    animation_prompts: list[str],
    output_dir: Path,
    clip_duration: int,
    video_resolution: Optional[str] = None,
) -> list[VideoArtifact]:
    """Generate Seedance clips chaining consecutive images.

    Args:
        video_resolution: if set, overrides the default video resolution
            (e.g. "480p" for smoke tests).
    """
    assert len(images) >= 2 and len(images) <= 4, (
        f"Expected 2-4 images, got {len(images)}"
    )
    assert len(animation_prompts) == len(images) - 1, (
        f"Expected {len(images) - 1} animation prompts for {len(images)} images, "
        f"got {len(animation_prompts)}"
    )
    artifacts: list[VideoArtifact] = []

    resolution_kwargs = {"resolution": video_resolution} if video_resolution else {}

    for i, anim_prompt in enumerate(animation_prompts):
        clip_num = i + 1
        label = f"{phase_label}_clip_{clip_num}"
        start_img = images[i]
        end_img = images[i + 1]
        local_path = output_dir / "clips" / f"{label}.mp4"

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


def run_single_pipeline(
    intent: str,
    mode: str,
    output_dir: Path,
    clip_duration: int,
    preserve_audio: bool = True,
    smoke: bool = False,
    mini: bool = False,
) -> RunManifest:
    """
    Run one full end-to-end pipeline (one of the two modes) and return its manifest.

    `mode` here is 'hero' or 'direct' — singular modes (not 'both'). 'both' is
    handled by the outer main() by running this function twice.

    When `smoke=True`, the pipeline is abbreviated to a minimum-cost smoke test:
    only Phase A scenes 1-2 and one clip (scene1->scene2) are generated, at 480p
    using the standard Seedance endpoint. Phase B is skipped entirely. This is
    the "does the plumbing work" test — ~$0.70 instead of ~$22.

    When `mini=True`, the full pipeline runs (both phases, all 8 images, 6 clips,
    ffmpeg stitch) but at 480p. Cost: ~$4 instead of ~$22. Use this to validate
    the Phase A -> Phase B transition and ffmpeg concat before a full-res run.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = RunManifest(started_at=time.time(), intent=intent, mode=mode)

    video_resolution = "480p" if (smoke or mini) else None
    if smoke:
        log.info("[smoke] Using 480p resolution for video generation")
    elif mini:
        log.info("[mini] Full pipeline at 480p resolution")

    # --- Step 1: (hero mode only) generate a hero concept image ---------------
    hero_url: Optional[str] = None
    hero_path: Optional[Path] = None
    if mode == "hero":
        log.info("[hero mode] Generating hero concept prompt via Claude")
        hero_prompt = claude_client.generate_hero_concept_prompt(intent)
        log.info("[hero mode] Hero prompt: %s", hero_prompt)

        hero_path = output_dir / "images" / "hero_concept.png"
        log.info("[hero mode] Rendering hero concept image")
        hero_path, hero_url = wavespeed_client.generate_image(
            prompt=hero_prompt,
            output_path=hero_path,
            reference_image_url=None,
        )
        manifest.hero_image = ImageArtifact(
            scene_label="hero_concept",
            prompt=hero_prompt,
            reference_image_url=None,
            local_path=str(hero_path),
            remote_url=hero_url,
        )

    # --- Step 2: ask Claude for the full prompt bundle ------------------------
    claude_mode = "hero_reverse" if mode == "hero" else "direct"
    log.info("[%s mode] Calling Claude for prompt bundle", mode)
    bundle = claude_client.generate_prompts(
        intent=intent,
        mode=claude_mode,
        hero_image_path=hero_path,
    )
    manifest.claude_response = bundle

    log.info("[%s mode] Got prompts: %d Phase A scenes, %d Phase A anims, %d Phase B scenes, %d Phase B anims",
             mode,
             len(bundle["phase_a"]["scene_prompts"]),
             len(bundle["phase_a"]["animation_prompts"]),
             len(bundle["phase_b"]["scene_prompts"]),
             len(bundle["phase_b"]["animation_prompts"]))

    # --- Step 3: Phase A images -----------------------------------------------
    # In hero mode: Scene 4 should be anchored on the hero image. We do that by
    # replacing Scene 4's generation with an /edit using the hero as reference.
    # Simpler: we treat scenes 1-3 normally (chained), and generate scene 4 as
    # an /edit using scene 3 as the ref (which is how the chain works), and
    # ALSO injecting hero continuity via the prompt.
    # For now we keep the simple chain — Claude's reverse-engineered prompts
    # already encode the hero composition.
    #
    # In smoke mode: only generate the first 2 scenes and the first clip.
    phase_a_scene_prompts = bundle["phase_a"]["scene_prompts"]
    phase_a_anim_prompts = bundle["phase_a"]["animation_prompts"]
    if smoke:
        phase_a_scene_prompts = phase_a_scene_prompts[:2]
        phase_a_anim_prompts = phase_a_anim_prompts[:1]
        log.info("[smoke] Truncating Phase A to 2 scenes + 1 clip")

    log.info("=" * 70)
    log.info("[%s mode] Generating Phase A scene images", mode)
    manifest.phase_a_images = _generate_phase_images(
        phase_label="phase_a",
        scene_prompts=phase_a_scene_prompts,
        output_dir=output_dir,
    )

    # --- Step 4: Phase A clips ------------------------------------------------
    log.info("=" * 70)
    log.info("[%s mode] Generating Phase A clips", mode)
    manifest.phase_a_clips = _generate_phase_clips(
        phase_label="phase_a",
        images=manifest.phase_a_images,
        animation_prompts=phase_a_anim_prompts,
        output_dir=output_dir,
        clip_duration=clip_duration,
        video_resolution=video_resolution,
    )

    # --- Step 5: Phase B images, anchored on Phase A scene 4 ------------------
    if smoke:
        log.info("[smoke] Skipping Phase B entirely")
    else:
        log.info("=" * 70)
        log.info("[%s mode] Generating Phase B scene images", mode)
        phase_a_last_url = manifest.phase_a_images[-1].remote_url
        manifest.phase_b_images = _generate_phase_images(
            phase_label="phase_b",
            scene_prompts=bundle["phase_b"]["scene_prompts"],
            output_dir=output_dir,
            anchor_image_url=phase_a_last_url,
        )

        # --- Step 6: Phase B clips ------------------------------------------------
        log.info("=" * 70)
        log.info("[%s mode] Generating Phase B clips", mode)
        manifest.phase_b_clips = _generate_phase_clips(
            phase_label="phase_b",
            images=manifest.phase_b_images,
            animation_prompts=bundle["phase_b"]["animation_prompts"],
            output_dir=output_dir,
            clip_duration=clip_duration,
            video_resolution=video_resolution,
        )

    # --- Step 7: Stitch all clips ---------------------------------------------
    log.info("=" * 70)
    log.info("[%s mode] Stitching final video", mode)
    all_clips = [Path(c.local_path) for c in manifest.phase_a_clips + manifest.phase_b_clips]
    final_path = output_dir / "final.mp4"
    if len(all_clips) == 1:
        # Smoke mode: only 1 clip, no need to invoke ffmpeg at all.
        log.info("[smoke] Only 1 clip, copying it to final.mp4 without stitching")
        import shutil
        shutil.copy2(all_clips[0], final_path)
    else:
        stitch_video.stitch_clips(all_clips, final_path, preserve_audio=preserve_audio)
    manifest.final_video = str(final_path)

    manifest.finished_at = time.time()
    manifest.save(output_dir / "manifest.json")
    return manifest


# --- CLI ---------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a viral restoration/bunker timelapse video end-to-end.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--intent", required=True,
        help="Free-form description of the subject, vibe, and lighting. "
             "Example: 'hidden survival bunker under a suburban backyard patio, "
             "overcast daylight, hatch opens to reveal warm interior glow'",
    )
    parser.add_argument(
        "--mode", choices=["hero", "direct", "both"], default="both",
        help="hero = generate a hero concept image first, then reverse-engineer scenes. "
             "direct = generate scenes straight from intent, no hero. "
             "both = run both pipelines so you can compare (default).",
    )
    parser.add_argument(
        "--output-dir", required=True, type=Path,
        help="Directory to write all artifacts into.",
    )
    parser.add_argument(
        "--clip-duration", type=int, default=DEFAULT_CLIP_DURATION,
        choices=[5, 10, 15],
        help=f"Seconds per clip (Seedance 2.0 only accepts 5, 10, or 15 — default {DEFAULT_CLIP_DURATION}).",
    )
    parser.add_argument(
        "--no-audio", action="store_true",
        help="Strip Seedance 2.0's native audio from the final stitched MP4. "
             "By default audio is preserved — the ambient/ASMR-style audio "
             "adds punch to the reveal. Pass this flag for a silent final cut.",
    )
    parser.add_argument(
        "--smoke", action="store_true",
        help="Smoke-test mode: generate only Phase A scenes 1-2 and one clip "
             "(scene1->scene2) at 480p. Skips Phase B entirely. Cost: ~$0.70. "
             "Use this to verify the pipeline end-to-end before committing to "
             "a full run.",
    )
    parser.add_argument(
        "--mini", action="store_true",
        help="Mini mode: run the full pipeline (both phases, 8 images, 6 clips, "
             "ffmpeg stitch) but at 480p. Cost: ~$4 instead of ~$22. Use this "
             "to validate Phase A -> Phase B transition and ffmpeg concat before "
             "a full-res run.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging.",
    )

    args = parser.parse_args()

    if args.smoke and args.mini:
        parser.error("--smoke and --mini are mutually exclusive")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)

    preserve_audio = not args.no_audio

    if args.mode == "both":
        hero_dir = args.output_dir / "hero_mode"
        direct_dir = args.output_dir / "direct_mode"
        log.info("Running BOTH modes — this will take ~2x as long.")
        log.info("Output: hero -> %s", hero_dir)
        log.info("Output: direct -> %s", direct_dir)

        hero_manifest = run_single_pipeline(
            args.intent, "hero", hero_dir, args.clip_duration, preserve_audio,
            smoke=args.smoke, mini=args.mini,
        )
        direct_manifest = run_single_pipeline(
            args.intent, "direct", direct_dir, args.clip_duration, preserve_audio,
            smoke=args.smoke, mini=args.mini,
        )

        print("\n" + "=" * 70)
        print("DONE. Two final videos generated for comparison:")
        print(f"  hero:   {hero_manifest.final_video}")
        print(f"  direct: {direct_manifest.final_video}")
        print("=" * 70)
    else:
        manifest = run_single_pipeline(
            args.intent, args.mode, args.output_dir, args.clip_duration, preserve_audio,
            smoke=args.smoke, mini=args.mini,
        )
        print("\n" + "=" * 70)
        print(f"DONE. Final video: {manifest.final_video}")
        print(f"Manifest: {args.output_dir / 'manifest.json'}")
        print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
