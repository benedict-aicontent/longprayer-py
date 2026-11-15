"""Two-pass EBU R128 loudness normalisation, shared by the voiceover and music features."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from core import ffmpeg_tools as ff
from core.context import Context


def parse_loudnorm(stderr: str) -> dict | None:
    match = re.search(r'\{\s*"input_i".*?\}', stderr, flags=re.S)
    if not match:
        return None
    try:
        raw = json.loads(match.group(0))
        values = {k: float(raw[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    except (ValueError, KeyError, json.JSONDecodeError):
        return None
    return values if all(math.isfinite(v) for v in values.values()) else None


def loudnorm_file(ctx: Context, src: Path, dst: Path, n: dict, post: list[str], label: str) -> None:
    """Measure `src`, then scale it to n['target_lufs'] (linear gain). Writes a 48 kHz stereo wav.

    `n` needs target_lufs, true_peak_db and loudness_range. `post` are extra filters to append.
    """
    base = f"loudnorm=I={n['target_lufs']:g}:TP={n['true_peak_db']:g}:LRA={n['loudness_range']:g}"
    report = ff.run_capture(
        ctx.ffmpeg, ["-i", str(src), "-af", base + ":print_format=json", "-f", "null", "-"],
        label=f"measure loudness ({label})", loglevel="info",
    )
    m = parse_loudnorm(report)
    if m:
        ctx.logger.debug("Loudness of %s: %.1f LUFS, true peak %.1f dB", label, m["input_i"], m["input_tp"])
        # loudnorm only scales linearly (no pumping, keeps dynamics) when the target LRA is not
        # lower than the measured one, so never ask for less than what the audio already has.
        lra = max(float(n["loudness_range"]), m["input_lra"] + 0.5)
        base = f"loudnorm=I={n['target_lufs']:g}:TP={n['true_peak_db']:g}:LRA={lra:.1f}"
        if m["input_tp"] + (float(n["target_lufs"]) - m["input_i"]) > float(n["true_peak_db"]):
            ctx.logger.info(
                "%s has loud peaks: reaching the target loudness would exceed %g dBTP, so the "
                "result may land slightly below the target level to keep peaks under that limit.",
                label, n["true_peak_db"],
            )
        applied = (
            f"{base}:measured_I={m['input_i']:.2f}:measured_TP={m['input_tp']:.2f}"
            f":measured_LRA={m['input_lra']:.2f}:measured_thresh={m['input_thresh']:.2f}"
            f":offset={m['target_offset']:.2f}:linear=true"
        )
    else:
        ctx.logger.warning("Could not measure loudness of %s (silent audio?); using single-pass.", label)
        applied = base
    chain = ",".join([applied, "aresample=48000", *post])
    ff.run_capture(
        ctx.ffmpeg,
        ["-i", str(src), "-af", chain, "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(dst)],
        label=f"normalize loudness ({label})",
    )
