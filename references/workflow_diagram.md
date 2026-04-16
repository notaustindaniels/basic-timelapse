# Workflow Diagram — Scene Chaining

This is the most important diagram to internalize before editing the orchestrator. Getting the scene-chaining wrong is the single most common failure mode.

## The full two-phase graph

```
                            ┌─────────────────────────────────────────────┐
                            │              PHASE A (above-ground)          │
                            └─────────────────────────────────────────────┘

                                           intent (+ hero?)
                                                  │
                                                  ▼
                                          Claude prompts
                                      (4 scenes + 3 anim)
                                                  │
            ┌─────────────────────────┬───────────┴───────────┬─────────────────────────┐
            ▼                         ▼                       ▼                         ▼
      ┌──────────┐              ┌──────────┐            ┌──────────┐              ┌──────────┐
      │ A-Scene1 │              │ A-Scene2 │            │ A-Scene3 │              │ A-Scene4 │
      │  (t2i)   │              │  (edit)  │            │  (edit)  │              │  (edit)  │
      │  NB2     │              │  NB2     │            │  NB2     │              │  NB2     │
      └────┬─────┘              └────┬─────┘            └────┬─────┘              └────┬─────┘
           │                         ▲                       ▲                         ▲
           │ ref                     │ ref                   │ ref                     │ ref
           └─────────────────────────┘                       │                         │
                                     └───────────────────────┘                         │
                                                             └─────────────────────────┘

      (A-Scene N+1 is always generated as an EDIT using A-Scene N as the single reference image.
       This replicates the transcript's "click the Reference button" step.)

                             ┌──────────────────────────────────┐
                             │   Seedance clips (3 per phase)    │
                             └──────────────────────────────────┘

           A-Scene1 ──┐                A-Scene2 ──┐                A-Scene3 ──┐
                      ├── A-Clip1              ├── A-Clip2                    ├── A-Clip3
           A-Scene2 ──┘                A-Scene3 ──┘                A-Scene4 ──┘
              start=Scene1                  start=Scene2                  start=Scene3
              end=Scene2                    end=Scene3                    end=Scene4
              prompt=anim[0]                prompt=anim[1]                prompt=anim[2]


                            ┌─────────────────────────────────────────────┐
                            │           PHASE B (underground / interior)   │
                            └─────────────────────────────────────────────┘

                           A-Scene4 (carried forward as narrative anchor)
                                                  │
                                                  ▼
                                    Claude Phase-B prompts
                                       (4 scenes + 3 anim)
                                                  │
                         [same 4-scene edit chain + 3 Seedance clips as Phase A]

                            ┌─────────────────────────────────────────────┐
                            │           ffmpeg concat (6 clips total)      │
                            └─────────────────────────────────────────────┘

          A-Clip1 → A-Clip2 → A-Clip3 → B-Clip1 → B-Clip2 → B-Clip3 → FINAL.mp4
```

## Why the edit chain (not independent t2i)

Generating each scene as an independent text-to-image call would give 4 different compositions even with identical prompts — Nano Banana 2's seed drift plus scene-by-scene framing variance would break the tripod-lock that makes the timelapse look real.

Using the `edit` endpoint with the previous scene as a reference tells NB2: "keep this framing, change only what the prompt describes." This is exactly what the transcript's narrator does when they click "Reference" in Higgsfield before generating scene 2, then scene 3, then scene 4.

## Why phase-A-scene-4 anchors phase B

The viral payoff is the reveal — the camera descends into a space that was *already established* by Phase A's final shot. Phase B's scene 1 should feel like a one-cut continuation of Phase A's scene 4, with the same lighting tone, same architectural details, just a new vantage point (usually inside / below).

Two ways to enforce this:

1. **Hero-mode**: Claude sees a hero concept image, which we use as A-Scene4's target. Then for Phase B, we pass A-Scene4 back to Claude as context so its Phase B scene 1 prompt references the established geometry.

2. **Direct-mode**: Claude generates all 8 scene prompts in one shot, so it can cross-reference A-scene4 and B-scene1 in its own output.

Either way, the key rule is: **Phase B scene 1 is generated as an edit using A-scene4 as the reference image**, not as a fresh t2i call.

## Animation prompt mapping

| Clip       | Start frame | End frame   | Prompt source             |
|------------|-------------|-------------|---------------------------|
| A-Clip 1   | A-Scene 1   | A-Scene 2   | phase_a.animation_prompts[0] |
| A-Clip 2   | A-Scene 2   | A-Scene 3   | phase_a.animation_prompts[1] |
| A-Clip 3   | A-Scene 3   | A-Scene 4   | phase_a.animation_prompts[2] |
| B-Clip 1   | B-Scene 1   | B-Scene 2   | phase_b.animation_prompts[0] |
| B-Clip 2   | B-Scene 2   | B-Scene 3   | phase_b.animation_prompts[1] |
| B-Clip 3   | B-Scene 3   | B-Scene 4   | phase_b.animation_prompts[2] |

Six clips total. At 5s each = 30s final video. If the user wants longer, bump individual clip durations up to 10s in `generate_timelapse.py` via the `--clip-duration` flag.
