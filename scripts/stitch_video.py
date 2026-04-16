"""
ffmpeg stitching.

We use the concat demuxer with `-c copy` when all inputs are encoded identically
(same codec, resolution, fps, audio codec, sample rate) — that's the fast path.
Seedance outputs are consistent within a single run, so -c copy usually works.
We fall back to a re-encode path if the concat-copy fails.

Audio handling: Seedance 2.0 always generates native audio (dialogue, ambient,
sound effects). We preserve that audio in the stitched output by default because
the ambient audio gives timelapse reveals real punch. If you'd rather have a
silent final cut, pass `preserve_audio=False` to stitch_clips() — it'll add the
`-an` flag to both ffmpeg paths.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class FfmpegError(RuntimeError):
    pass


def _check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise FfmpegError(
            "ffmpeg not found on PATH. Install it first: `brew install ffmpeg` on "
            "macOS, `apt install ffmpeg` on Debian/Ubuntu."
        )


def stitch_clips(
    clip_paths: list[Path],
    output_path: Path,
    *,
    preserve_audio: bool = True,
) -> Path:
    """
    Concatenate a list of MP4 clips into one MP4.

    Tries stream-copy first (fast, no quality loss). If that fails (e.g. codec
    mismatch across inputs), re-encodes with libx264 + AAC.

    Args:
        clip_paths: input MP4 files in order.
        output_path: destination MP4.
        preserve_audio: when True (default), keep Seedance 2.0's native audio
            track in the stitched output. Set False for a silent final cut.
    """
    _check_ffmpeg()

    if not clip_paths:
        raise ValueError("stitch_clips: no input clips")

    for p in clip_paths:
        if not p.exists():
            raise FileNotFoundError(f"Clip not found: {p}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write a concat list file
    list_file = output_path.parent / f"{output_path.stem}_concat.txt"
    with open(list_file, "w") as f:
        for p in clip_paths:
            # ffmpeg's concat format requires paths be absolute or relative to the list file.
            # We use absolute paths and escape single quotes.
            escaped = str(p.resolve()).replace("'", r"'\''")
            f.write(f"file '{escaped}'\n")

    audio_copy_flag = ["-an"] if not preserve_audio else []

    # --- Attempt 1: stream copy (fast, no re-encode) -------------------------
    # `-c copy` without `-an` keeps both video and audio streams if present.
    # This requires that all inputs have matching audio codec params too (not
    # just video). Seedance 2.0 outputs are consistent within one run, so this
    # usually holds; the re-encode fallback handles cases where it doesn't.
    copy_cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        *audio_copy_flag,
        str(output_path),
    ]
    log.info("Stitching (stream copy, audio=%s): %s",
             "preserved" if preserve_audio else "stripped",
             " ".join(copy_cmd))
    result = subprocess.run(copy_cmd, capture_output=True, text=True)

    if result.returncode == 0 and output_path.exists():
        log.info("Stitched → %s (%d bytes)", output_path, output_path.stat().st_size)
        list_file.unlink(missing_ok=True)
        return output_path

    log.warning(
        "Stream copy failed (exit %d). stderr:\n%s\nFalling back to re-encode.",
        result.returncode, result.stderr[-2000:],
    )

    # --- Attempt 2: re-encode ------------------------------------------------
    audio_encode_flags = (
        ["-an"] if not preserve_audio
        else ["-c:a", "aac", "-b:a", "192k"]
    )
    encode_cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",           # visually lossless-ish
        "-pix_fmt", "yuv420p",  # broad playback compatibility
        *audio_encode_flags,
        str(output_path),
    ]
    log.info("Stitching (re-encode, audio=%s): %s",
             "preserved" if preserve_audio else "stripped",
             " ".join(encode_cmd))
    result = subprocess.run(encode_cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise FfmpegError(
            f"ffmpeg re-encode failed (exit {result.returncode}):\n{result.stderr[-2000:]}"
        )

    log.info("Stitched → %s (%d bytes)", output_path, output_path.stat().st_size)
    list_file.unlink(missing_ok=True)
    return output_path
