"""Runs the enabled features (stages) in order.

Each stage reads ctx.video (the previous stage's output) and sets ctx.video to its own result.
New features are added by importing the module and appending to STAGES.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from core.context import Context
from features import captions, clips, music, onscreen_text, voiceover

# (feature flag in config.json, label, function). The voiceover is prepared FIRST because its
# length decides how long the clip video must be. Music is also prepared before the long render,
# so a missing music file stops the run immediately. Captions and on-screen text are also prepared first (speech recognition is
# slow and must not fail after a long render); they are burned into the video during the clip render.
# Audio is mixed into the video AFTER the clips.
STAGES = [
    ("voiceover", "voiceover: prepare", voiceover.prepare),
    ("music", "music: prepare", music.prepare),
    ("captions", "captions: prepare", captions.prepare),
    ("onscreen_text", "on-screen text: prepare", onscreen_text.prepare),
    ("clips", "clips", clips.run),
    ("voiceover", "voiceover: mix", voiceover.mix),
    ("music", "music: mix", music.mix),
]


def run_pipeline(ctx: Context) -> Path | None:
    enabled = ctx.cfg["features"]
    announced: set[str] = set()
    for flag_name, label, stage in STAGES:
        if not enabled.get(flag_name, False):
            if flag_name not in announced:
                ctx.logger.info("Feature '%s' is disabled in config.json - skipped.", flag_name)
                announced.add(flag_name)
            continue
        ctx.logger.info("=== %s ===", label)
        stage(ctx)
    return ctx.video


def next_output_path(output_dir: Path, prefix: str, digits: int) -> Path:
    """video_001.mp4, video_002.mp4 ... (highest existing number + 1)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)\.mp4$", re.IGNORECASE)
    numbers = [int(m.group(1)) for p in output_dir.iterdir() if (m := pattern.match(p.name))]
    return output_dir / f"{prefix}{(max(numbers) + 1 if numbers else 1):0{digits}d}.mp4"


def finalize(ctx: Context) -> Path | None:
    """Move the finished video into the output folder with an auto-incremented name."""
    if ctx.video is None:
        return None
    if ctx.video.suffix.lower() != ".mp4":
        raise RuntimeError(f"The last stage produced {ctx.video.name}, not an .mp4. Check that the audio stages ran.")
    out_cfg = ctx.cfg["output"]
    destination = next_output_path(ctx.paths["output"], str(out_cfg["name_prefix"]), int(out_cfg["digits"]))
    shutil.move(str(ctx.video), str(destination))
    ctx.video = destination
    return destination
