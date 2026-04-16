# Example Run — Walkthrough

This walks through what a `--mode both` run looks like for the intent used in the original YouTube transcript.

## Command

```bash
python scripts/generate_timelapse.py \
  --intent "hidden survival bunker under a suburban backyard patio with wooden fence and forest backdrop, overcast daylight, final scene shows hatch open revealing warm amber interior glow" \
  --mode both \
  --output-dir ./runs/bunker_demo
```

## What happens, in order

```
bunker_demo/
├── hero_mode/         ← pipeline 1: hero concept first
└── direct_mode/       ← pipeline 2: direct from intent
```

### Hero mode timeline

1. **Hero prompt generation** (~5s)
   Claude produces a single text-to-image prompt for the final "money shot" — what Phase A scene 4 should look like.
   > *"Static tripod shot at 1.6m, 35mm lens, centered on a suburban backyard patio with a wooden fence and dense forest backdrop. An open rectangular concrete hatch in the patio reveals warm amber light spilling up the stairs from below. Overcast daylight, soft shadows, potted plants flanking the opening, stainless grill to the right. Cinematic contrast between cool outdoor gray and warm interior glow."*

2. **Hero image render** (~10s)
   `images/hero_concept.png` appears. You can preview it — if you hate it, kill the run and try again with a different intent.

3. **Prompt bundle** (~8s)
   Claude reverse-engineers 4 Phase A scene prompts + 3 animation prompts, and 4 Phase B scene prompts + 3 animation prompts, all anchored to the hero composition.

4. **Phase A scene 1** (text-to-image, ~10s) — untouched patio, no hatch
5. **Phase A scenes 2-4** (edit chain, ~10s each, ~30s total) — construction, unstaged, staged
6. **Phase A clips 1-3** (Seedance i2v 1080p 5s each, ~60-120s each, ~5min total)
7. **Phase B scene 1** (edit using A-scene-4 as reference) — excavated pit
8. **Phase B scenes 2-4** (edit chain) — rebar/pour, finished empty, fully staged
9. **Phase B clips 1-3** (Seedance)
10. **Stitch** (~3s) — ffmpeg concat of 6 clips → `final.mp4`

### Direct mode timeline

Same as hero mode, but skipping steps 1-2. Claude generates all 8 scene prompts from the intent without seeing any rendered image first.

## Expected manifest.json excerpt

```json
{
  "started_at": 1744818121.4,
  "intent": "hidden survival bunker under a suburban backyard patio...",
  "mode": "hero",
  "claude_response": {
    "hero_concept_prompt": "Static tripod shot at 1.6m...",
    "phase_a": {
      "label": "Above-ground transformation",
      "scene_prompts": [
        "SCENE LOCK: static tripod at eye-level 1.6m, natural 35mm lens...",
        "SCENE LOCK: static tripod at same 1.6m height, same 35mm lens...",
        "...",
        "..."
      ],
      "animation_prompts": [
        "locked-off camera, workers remove patio stones, mini excavator digs...",
        "workers tie rebar, pour concrete walls, forms removed...",
        "hatch installed and opened, interior lights flicker on..."
      ]
    },
    "phase_b": { "...": "..." }
  },
  "hero_image": {
    "scene_label": "hero_concept",
    "prompt": "...",
    "local_path": "runs/bunker_demo/hero_mode/images/hero_concept.png",
    "remote_url": "https://static.wavespeed.ai/out/abc.png"
  },
  "phase_a_images": [
    { "scene_label": "phase_a_scene_1", "prompt": "...", "reference_image_url": null, "...": "..." },
    { "scene_label": "phase_a_scene_2", "prompt": "...", "reference_image_url": "https://.../phase_a_scene_1.png", "...": "..." },
    "..."
  ],
  "phase_a_clips": [
    {
      "clip_label": "phase_a_clip_1",
      "start_image_url": "https://.../phase_a_scene_1.png",
      "end_image_url": "https://.../phase_a_scene_2.png",
      "animation_prompt": "locked-off camera, workers remove patio stones...",
      "duration": 5,
      "local_path": "runs/bunker_demo/hero_mode/clips/phase_a_clip_1.mp4",
      "remote_url": "https://static.wavespeed.ai/out/xyz.mp4"
    },
    "..."
  ],
  "final_video": "runs/bunker_demo/hero_mode/final.mp4",
  "finished_at": 1744818792.1
}
```

## Tips from early runs

- **If Phase B feels disconnected from Phase A**, your intent was too vague about the transition moment. Say explicitly "hatch opens to reveal interior" in the intent.
- **If the timelapse feels jittery**, bump `--clip-duration` to 8 or 10. Seedance has more frames to interpolate motion smoothly.
- **If you want to regenerate just one scene**, grab its prompt from `manifest.json`, tweak it, and call `wavespeed_client.generate_image(...)` manually with the same `reference_image_url`. Drop the new PNG over the old one, then re-run the affected clip with `wavespeed_client.generate_video(...)` and re-stitch.
- **Hero vs direct**: in early testing, hero mode tends to produce a more photogenic final scene (because it's a rendered target, not a text prompt), while direct mode tends to feel more "constructed" and narrative. Run both the first few times and see which style you prefer.
