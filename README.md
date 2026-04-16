# Bunker Timelapse Skill

Programmatically generate viral "hidden bunker" / restoration timelapse videos end-to-end.

This skill replaces the manual web-UI workflow from the YouTube transcript (Pinterest → ChatGPT → Higgsfield → Kling → back and forth) with a single command. It orchestrates:

- **Claude** (via your subscription's OAuth token) — prompt engineering
- **Nano Banana 2** (via WaveSpeed) — image generation with scene-to-scene continuity
- **Seedance v1.5 Pro** (via WaveSpeed) — image-to-video at 1080p with start/end frame control
- **ffmpeg** — stitching the 6 clips into one final MP4

## What it produces

For any intent like *"hidden survival bunker under a suburban backyard patio, overcast daylight"*, you get:

- 8 PNG scene images (4 above-ground phase + 4 underground phase)
- 6 MP4 video clips (3 per phase, each chaining scene N → scene N+1)
- 1 final stitched MP4 (30 seconds at default 5s/clip)
- A `manifest.json` documenting every prompt, every API call, and every artifact path

Optionally also a hero concept image if you use `--mode hero` or `--mode both`.

---

## Install

### 1. Clone and set up Python

```bash
git clone <this-repo> bunker-timelapse-skill
cd bunker-timelapse-skill

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

Requires Python 3.10+.

### 2. Install ffmpeg

```bash
# macOS
brew install ffmpeg

# Debian / Ubuntu
sudo apt install ffmpeg

# Verify
ffmpeg -version
```

### 3. Get a WaveSpeed API key

1. Sign up at https://wavespeed.ai
2. Go to the API Keys section of the dashboard
3. Create a new key, copy it

### 4. Get a Claude OAuth token (recommended path)

If you have a Claude Pro, Max, Team, or Enterprise subscription, this uses your existing plan's included usage — no separate API bill.

```bash
# Install Claude Code if you don't have it yet
npm install -g @anthropic-ai/claude-code

# Log in (opens a browser once)
claude login

# Generate a long-lived OAuth token (~1 year validity)
claude setup-token
```

The `setup-token` command prints a token to the terminal. **Copy it immediately** — it's not saved anywhere.

> **No subscription?** You can use an Anthropic API key instead (`ANTHROPIC_API_KEY`). This falls back to pay-per-token billing on the standard API. See Option B below.

---

## Configure your API keys

Two supported approaches. Pick whichever fits your workflow — both are documented because both are common in the wild.

### Option A — `~/.zshenv` exports (what you're already doing)

This is what Unix/backend engineers tend to use on their personal dev machines. Secrets live outside any project directory and are auto-loaded by every shell.

```bash
cat >> ~/.zshenv << 'EOF'
export WAVESPEED_API_KEY="ws_your_key_here"
export CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat-your_token_here"
EOF

# Reload the current shell so the exports take effect
source ~/.zshenv

# Verify
echo $WAVESPEED_API_KEY
echo $CLAUDE_CODE_OAUTH_TOKEN
```

If you're on `bash`, use `~/.bashrc` or `~/.bash_profile` instead.

### Option B — `.env` file in the project directory

This is the pattern most "enterprise" Python codebases use because multiple collaborators can share a project without leaking each other's secrets via shell config. It needs `python-dotenv` (already in `requirements.txt`) and the file is git-ignored.

```bash
cp .env.example .env

# Edit .env and fill in the blanks:
#   WAVESPEED_API_KEY=ws_...
#   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-...
```

**Don't do both.** If both are set, the shell environment wins (that's standard precedence). Pick one and stick with it.

### Option B-alt — API key instead of OAuth

If you don't have a Claude subscription or prefer pay-per-token billing, swap the Claude line for:

```bash
# ~/.zshenv
export ANTHROPIC_API_KEY="sk-ant-api03-..."

# or .env
ANTHROPIC_API_KEY=sk-ant-api03-...
```

The code uses whichever Claude credential is present, preferring OAuth when both exist.

---

## Usage

Basic:

```bash
python scripts/generate_timelapse.py \
  --intent "hidden survival bunker under a suburban backyard patio, overcast daylight, hatch opens to reveal warm interior glow" \
  --output-dir ./runs/first_run
```

Default `--mode` is `both`, which runs the hero-first pipeline and the direct-from-intent pipeline in sequence so you can compare. To run only one:

```bash
# Generate a hero concept image first, then reverse-engineer the 4 scenes from it
python scripts/generate_timelapse.py \
  --intent "..." \
  --mode hero \
  --output-dir ./runs/hero_only

# Skip the hero step — generate 4 scenes directly from intent
python scripts/generate_timelapse.py \
  --intent "..." \
  --mode direct \
  --output-dir ./runs/direct_only
```

Longer clips (smoother motion, more cost):

```bash
python scripts/generate_timelapse.py \
  --intent "..." \
  --output-dir ./runs/long \
  --clip-duration 10    # Seedance 2.0 accepts only 5, 10, or 15
```

> Note: Seedance 2.0 always generates native audio (ambient, sound effects, and occasionally dialogue). This skill **preserves that audio in the final stitched MP4 by default** because the ambient audio gives timelapse reveals a lot of punch. Pass `--no-audio` if you'd rather have a silent final cut.
>
> Caveat: because each clip's audio is generated independently by Seedance, you'll hear discontinuities (volume pops, ambient-tone shifts) at every clip boundary — 5 of them in the stitched video. This is usually acceptable for construction-style ambience but jarring if the clips have continuous dialogue. If it's too jarring, either strip audio with `--no-audio` and drop in a single music/ambient bed in post, or add an ffmpeg `acrossfade` filter step (not included here).

Verbose logging if something's going wrong:

```bash
python scripts/generate_timelapse.py --intent "..." --output-dir ./runs/debug -v
```

### What you'll see during a run

```
15:42:01 orchestrator INFO: [hero mode] Generating hero concept prompt via Claude
15:42:08 orchestrator INFO: [hero mode] Hero prompt: A suburban backyard at overcast noon...
15:42:08 orchestrator INFO: [hero mode] Rendering hero concept image
15:42:09 wavespeed_client INFO: Submitting image gen: endpoint=/google/nano-banana-2/text-to-image ref=False
15:42:10 wavespeed_client INFO: Submitted request pred_abc123, polling for completion
15:42:18 wavespeed_client INFO: Image ready: https://... — downloading
...
15:47:22 orchestrator INFO: [hero mode] Stitching final video
15:47:24 orchestrator INFO: Stitched → ./runs/first_run/hero_mode/final.mp4 (42318441 bytes)
```

A full `--mode both` run takes roughly **8-15 minutes** depending on WaveSpeed load (images are ~10s each, videos are ~60-120s each).

### Resume / regenerate partial outputs

If scene 3 looks off, the manifest tells you every prompt. Edit the prompt in your copy of the JSON, then re-run just that scene manually through `wavespeed_client.generate_image()` in a Python REPL, drop the new PNG in place, and re-run the affected clip with `wavespeed_client.generate_video()`. There's no automated `--start-at` flag yet — it's on the roadmap below.

---

## Directory layout

```
bunker-timelapse-skill/
├── SKILL.md                          # Skill manifest (for Claude.ai skill loader)
├── README.md                          # This file
├── requirements.txt
├── .env.example
├── .gitignore
├── references/
│   ├── restoration_system_prompt.md   # Claude's system prompt — edit freely
│   ├── wavespeed_api.md              # WaveSpeed endpoint reference
│   └── workflow_diagram.md           # Scene-chaining diagram
└── scripts/
    ├── generate_timelapse.py         # ← main CLI entry point
    ├── claude_client.py              # Claude Agent SDK wrapper
    ├── wavespeed_client.py           # WaveSpeed submit + poll + download
    ├── stitch_video.py               # ffmpeg concat
    └── config.py                     # env loading + model constants
```

### Output layout (one run)

```
runs/first_run/
├── hero_mode/
│   ├── manifest.json                  # everything that happened
│   ├── images/
│   │   ├── hero_concept.png
│   │   ├── phase_a_scene_1.png  ...  phase_a_scene_4.png
│   │   └── phase_b_scene_1.png  ...  phase_b_scene_4.png
│   ├── clips/
│   │   ├── phase_a_clip_1.mp4  ...  phase_a_clip_3.mp4
│   │   └── phase_b_clip_1.mp4  ...  phase_b_clip_3.mp4
│   └── final.mp4                     # ← the stitched result
└── direct_mode/
    └── ...                           # same layout, no hero_concept.png
```

---

## Costs (approximate, April 2026)

Per full `--mode both` run at defaults (1080p, 5s clips, 2K images, no audio):

| Item | Count | Unit cost | Subtotal |
|------|-------|-----------|----------|
| NB2 text-to-image (2K) | 2 (hero + direct scene 1) | ~$0.03 | ~$0.06 |
| NB2 edit (2K) | 14 (4+4 chained scenes × 2 modes − 2 first-scenes) | ~$0.04 | ~$0.56 |
| Seedance 2.0 i2v 1080p 5s | 12 (6 clips × 2 modes) | ~$1.80 | **~$21.60** |
| Claude Opus | 2 calls (~5k tokens each) | subscription | $0 |
| **Total** | | | **~$22.22** |

Seedance 2.0 at 1080p is the dominant cost — that's the price of being on the current state-of-the-art model. Ways to cut the bill:

- **Single mode** (`--mode hero` or `--mode direct`) halves the cost to ~$11
- **720p** instead of 1080p drops Seedance cost by ~67% (~$7 total for `--mode both`)
- **Swap to `seedance-2.0-fast`** in `scripts/config.py` saves another 17–33%
- **Swap to `seedance-v1.5-pro`** — previous generation, still solid, ~$0.30/clip at 1080p → full `--mode both` run ~$4.20

Check https://wavespeed.ai/pricing for current rates.

---

## Troubleshooting

### "No Claude credential found"
Set either `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`. Re-source your shell or re-open the terminal.

### "WaveSpeed returned 401 Unauthorized"
`WAVESPEED_API_KEY` is missing, wrong, or expired. Regenerate in the WaveSpeed dashboard.

### "Prediction pred_xxx failed: content policy violation"
Nano Banana 2 rejected the prompt. The error names the failing prompt in the log. Edit the relevant scene prompt in `references/restoration_system_prompt.md` (to change future runs) or the generated `manifest.json` (to retry this run).

### "ffmpeg not found on PATH"
Install it per the Install section above. Verify with `ffmpeg -version`.

### Claude returns non-JSON output
The system prompt is strict but Claude occasionally wraps JSON in ` ```json ` fences. The client strips them automatically. If you see the validator fail with "must have exactly 4 entries", look at the raw response in the error and tighten the system prompt if needed.

### OAuth token expired
Long-lived tokens from `claude setup-token` last ~1 year, but if you used a short-lived one from `claude login` it expires in ~8 hours. Regenerate:
```bash
claude setup-token
# update WAVESPEED_API_KEY in ~/.zshenv or .env with the new token
```

### Scene drift (scenes don't match geometry)
Edit `references/restoration_system_prompt.md` and tighten the SCENE LOCK guidance. The orchestrator reads the file fresh on every run, so no redeploy needed.

---

## Roadmap

- `--start-at phase_a_scene_3` to resume from a specific scene without regenerating earlier ones
- Optional multi-reference mode — Seedance 2.0 accepts up to 4 reference images per clip, which could lock character/style continuity across all 6 clips even more tightly
- Variant switching flag (`--seedance fast|standard|turbo`) to toggle between the four Seedance 2.0 endpoints without editing `config.py`
- Optional upscaling pass with WaveSpeed's video upscaler for 4K finals
- Music bed overlay via ffmpeg (royalty-free track pulled in)

---

## How it maps to the original YouTube workflow

| Transcript step | Programmatic equivalent |
|---|---|
| "Search Pinterest for 'hidden bunker'" | Replaced: Claude generates a hero concept prompt, Nano Banana 2 renders it |
| "Upload image to Restoration Timelapse GPT" | `claude_client.generate_prompts(mode='hero_reverse', hero_image_path=...)` |
| "Copy prompt 1 into Higgsfield, generate" | `wavespeed_client.generate_image(prompt, reference_image_url=None)` — scene 1 |
| "Click Reference, paste prompt 2, generate" | `wavespeed_client.generate_image(prompt, reference_image_url=prev_url)` — scenes 2–4 |
| "Kling 3.0 — upload start frame, end frame, paste animation prompt" | `wavespeed_client.generate_video(start_url, end_url, anim_prompt)` via Seedance 2.0 (leaderboard leader, same start/end-frame control) |
| Repeat whole thing for underground phase | Phase B runs automatically, anchored on Phase A scene 4 |
| Manually download each clip and stitch in editor | `stitch_video.stitch_clips(all_clips, final_path)` |
