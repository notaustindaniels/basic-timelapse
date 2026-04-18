# Bunker Timelapse Skill

Programmatically generate viral "hidden bunker" / restoration timelapse videos end-to-end.

This skill replaces the manual web-UI workflow from the YouTube transcript (Pinterest → ChatGPT → Higgsfield → Kling → back and forth) with a single command. It orchestrates:

- **Claude** (via your subscription's OAuth token) — acts as the creative "Restoration Timelapse" GPT. Called twice per run: once for Phase A (above-ground), once for Phase B (underground reveal, with Phase A's final image attached as reference).
- **Nano Banana 2** (via WaveSpeed) — image generation with scene-to-scene continuity via the `/edit` endpoint.
- **Seedance 2.0** (via WaveSpeed) — image-to-video at 1080p with start/end frame control.
- **ffmpeg** — stitching the 7 clips into one final MP4.

The GPT's natural markdown output (IMAGE 1-4 / VIDEO 1-4 blocks in `text` code fences) is extracted by a deterministic regex parser — no LLM-as-parser, no hallucination risk between "GPT produces prompts" and "Nano Banana receives prompts."

## What it produces

For any intent like *"concealed below-grade retreat room under a suburban backyard patio"* (see note on terminology below), you get:

- 8 PNG scene images (4 Phase A above-ground + 4 Phase B underground)
- 1 optional closure image (only with `--closure protagonist`)
- 7 MP4 video clips (3 Phase A + 3 Phase B + 1 closure clip)
- 1 final stitched MP4 (35-40 seconds at default 5s/clip)
- Raw GPT responses in `phase_a_gpt_response.md` and `phase_b_gpt_response.md` (the markdown straight from Claude, useful for debugging)
- A `manifest.json` documenting every prompt, every API call, every artifact path

> **⚠️ A note on terminology:** This skill is colloquially called the "bunker timelapse" skill because that's the viral format it targets. But the word "bunker" (and related language like "hidden," "survival," "fortified") trips content moderation on the image model. Inside the pipeline, the orchestrator's `--intent` flag needs to use architectural/lifestyle language instead: "concealed basement room," "below-grade suite," "underground retreat." The visual result is the same; only the prompt wording changes. See `SKILL.md` Step 0.5 for the full translation table. Example commands in this README all use the moderation-safe phrasing.

## Closure modes

The final clip has two options, chosen at runtime via `--closure`:

- **`cinematic`** (default): the GPT's own VIDEO 4 block, rendered as a Seedance push-in on Phase B Scene 4. Camera zooms into the reveal. No characters.
- **`protagonist`**: the transcript narrator's method. Nano Banana 2 inserts a character into Phase B Scene 4, then Seedance animates the character settling into the space. Requires `--protagonist-description "..."` or `--protagonist-description auto`.

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

Basic (uses defaults — cinematic closure, no questionnaire answers passed):

```bash
python scripts/generate_timelapse.py \
  --intent "concealed below-grade retreat room under a suburban backyard patio, accessed via a flush-mounted hatch" \
  --output-dir ./runs/first_run
```

Recommended — pass answers to the GPT's mandatory questionnaire up front so the GPT doesn't pause mid-generation to ask:

```bash
python scripts/generate_timelapse.py \
  --intent "concealed below-grade retreat room under a suburban backyard patio, accessed via a flush-mounted hatch" \
  --vibe "rustic but modern" \
  --features "wooden fence, stainless grill, hidden hatch with warm interior glow" \
  --lighting "overcast daytime" \
  --closure cinematic \
  --output-dir ./runs/first_run
```

Protagonist-closure variant (the transcript narrator's style):

```bash
python scripts/generate_timelapse.py \
  --intent "concealed below-grade retreat room under a suburban backyard patio, accessed via a flush-mounted hatch" \
  --vibe rustic \
  --features "hatch with warm interior glow" \
  --lighting "overcast daytime" \
  --closure protagonist \
  --protagonist-description "the homeowner, mid-40s, denim and flannel, relaxed" \
  --output-dir ./runs/protagonist_run
```

Let the image model invent a character:

```bash
  --closure protagonist \
  --protagonist-description auto
```

Longer clips (smoother motion, more cost):

```bash
python scripts/generate_timelapse.py \
  --intent "..." \
  --clip-duration 10    # Seedance 2.0 accepts only 5, 10, or 15
```

Test flags (cheap):

```bash
--smoke   # 2 Phase A scenes + 1 clip at 480p. Phase B skipped.          ~$0.70
--mini    # full pipeline at 480p.                                       ~$5-6
```

> Seedance 2.0 always generates native audio (ambient, sound effects). This skill **preserves that audio in the final stitched MP4 by default.** Pass `--no-audio` for a silent cut. Note: each clip's audio is generated independently, so you'll hear discontinuities at every clip boundary (6 boundaries in a 7-clip final). Acceptable for construction ambience, jarring if dialogue is present.

Verbose logging:

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
│   ├── restoration_system_prompt.md   # The verbatim custom GPT system prompt
│   ├── wavespeed_api.md              # WaveSpeed endpoint reference
│   └── workflow_diagram.md           # Scene-chaining diagram
└── scripts/
    ├── generate_timelapse.py         # ← main CLI entry point
    ├── claude_client.py              # thin transport over Claude Agent SDK
    ├── prompt_parser.py              # regex parser for GPT markdown output
    ├── wavespeed_client.py           # NB2 + Seedance API client
    ├── stitch_video.py               # ffmpeg concat
    └── config.py                     # env loading + model constants
```

### Output layout (one run)

```
runs/first_run/
├── manifest.json                      # everything that happened
├── phase_a_gpt_response.md            # raw markdown from the Phase A GPT call
├── phase_b_gpt_response.md            # raw markdown from the Phase B GPT call
├── images/
│   ├── phase_a_scene_1.png  ...  phase_a_scene_4.png
│   ├── phase_b_scene_1.png  ...  phase_b_scene_4.png
│   └── closure_protagonist.png        # only with --closure protagonist
├── clips/
│   ├── phase_a_clip_1.mp4   ...  phase_a_clip_3.mp4
│   ├── phase_b_clip_1.mp4   ...  phase_b_clip_3.mp4
│   └── closure_cinematic.mp4 OR closure_protagonist.mp4
└── final.mp4                          # ← the stitched result (7 clips)
```

---

## Costs (approximate, April 2026)

Per full run at defaults (1080p, 5s clips, 2K images):

| Item | Cinematic closure | Protagonist closure |
|------|---|---|
| NB2 text-to-image (2K) | 1 (Phase A scene 1) | 1 |
| NB2 edit (2K) | 7 (Phase A 2-4, Phase B 1-4) | 8 (+ protagonist insertion) |
| Seedance 2.0 i2v 1080p 5s | 7 (6 chain + 1 push-in) | 7 (6 chain + 1 protagonist) |
| Claude Opus | 2 calls (Phase A + Phase B) | 2 |
| **Total** | **~$24** | **~$26** |

Seedance 2.0 at 1080p is the dominant cost. Ways to cut the bill:

- **`--mini`** runs the full pipeline at 480p for ~$5-6
- **720p** (edit `DEFAULT_VIDEO_RESOLUTION` in `scripts/config.py`) drops Seedance cost by ~67%
- **`--smoke`** is a minimal-cost plumbing test: 2 images + 1 clip at 480p, ~$0.70

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
| "Upload image to Restoration Timelapse GPT" (or no image, just a description) | `claude_client.send_prompt()` with the verbatim GPT system prompt, user intent + questionnaire answers, optional image attachment |
| "Copy prompt 1 into Higgsfield, generate" | `wavespeed_client.generate_image()` with no reference — scene 1 |
| "Click Reference, paste prompt 2, generate" | `wavespeed_client.generate_image()` with previous scene as reference — scenes 2-4 |
| "Kling 3.0 — upload start frame, end frame, paste animation prompt" | `wavespeed_client.generate_video()` via Seedance 2.0 — 3 clips per phase |
| "Generate Phase B by taking the final Phase A image back to the GPT" | Second `claude_client.send_prompt()` call with Phase A scene 4 attached |
| "Put the customer on the couch" (transcript's closing method) | `--closure protagonist --protagonist-description "..."` — NB2 edit to insert the character, then Seedance scene_4 → character |
| Manually download each clip and stitch in editor | `stitch_video.stitch_clips(all_clips, final_path)` |
