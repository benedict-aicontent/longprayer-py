"""Feature 3 - Background music.

Two stages (see pipeline.py):

  prepare(ctx)  runs BEFORE the long render. Picks one music file (random, or the one named in
                settings/music.json), converts it to 48 kHz stereo and normalizes its loudness.
                Problems with the music (empty folder, unreadable file) stop the run right away.
  mix(ctx)      runs AFTER the voiceover mix. Builds the music "bed" and adds it to the video's
                audio:
                  1. loop the music (with a crossfade) until it covers the video
                  2. fade in at the start and fade out at the end of the video
                  3. start it at `start_at_seconds`
                  4. set its level: `no_voice_db` normally, `under_voice_db` while the voiceover
                     speaks (ducking). Short gaps between voiceovers stay ducked so the music does
                     not pump up and down between sentences.
                  5. add it to the existing audio (voiceover and/or clip audio, which stay as they
                     are: the music never ducks the clip audio).

Levels are in dB RELATIVE TO THE VOICEOVER loudness: the music is normalized to the same reference
loudness as the voiceover (normalize.target_lufs), then `no_voice_db` / `under_voice_db` are applied.
"""
from __future__ import annotations

import math
from pathlib import Path

from core import ffmpeg_tools as ff
from core.config_loader import flag, load_feature_settings, num
from core.context import Context
from core.errors import ConfigError
from core.files import list_files
from core.loudness import loudnorm_file
from features import clips

SETTINGS_FILE = "music.json"
MAX_FILTER_CHARS = 28000
MAX_LOOP_COPIES = 300

DEFAULTS: dict = {
    "extensions": [".mp3", ".wav"],
    "file": None,
    "start_at_seconds": 0.0,
    "levels": {"under_voice_db": -22.0, "no_voice_db": -12.0},
    "ducking": {
        "enabled": True,
        "attack_seconds": 0.5,
        "release_seconds": 1.0,
        "keep_ducked_gaps_shorter_than_seconds": 3.0,
    },
    "fade": {"in_seconds": 3.0, "out_seconds": 8.0},
    "loop": {"crossfade_seconds": 2.0},
    "normalize": {"enabled": True, "target_lufs": -16.0, "true_peak_db": -1.5, "loudness_range": 11.0},
}


# --------------------------------------------------------------------------- settings
def validate_settings(s: dict) -> None:
    if not isinstance(s["extensions"], list) or not s["extensions"]:
        raise ConfigError("'extensions' must be a non-empty list such as [\".mp3\", \".wav\"].")
    if s["file"] is not None and not isinstance(s["file"], str):
        raise ConfigError("'file' must be null (random pick) or a file name such as \"calm.mp3\".")
    num(s["start_at_seconds"], "start_at_seconds", 0, 3600)
    num(s["levels"]["under_voice_db"], "levels.under_voice_db", -60, 12)
    num(s["levels"]["no_voice_db"], "levels.no_voice_db", -60, 12)
    d = s["ducking"]
    flag(d["enabled"], "ducking.enabled")
    num(d["attack_seconds"], "ducking.attack_seconds", 0.05, 10)
    num(d["release_seconds"], "ducking.release_seconds", 0.05, 10)
    num(d["keep_ducked_gaps_shorter_than_seconds"], "ducking.keep_ducked_gaps_shorter_than_seconds", 0, 600)
    num(s["fade"]["in_seconds"], "fade.in_seconds", 0, 120)
    num(s["fade"]["out_seconds"], "fade.out_seconds", 0, 120)
    num(s["loop"]["crossfade_seconds"], "loop.crossfade_seconds", 0, 30)
    n = s["normalize"]
    flag(n["enabled"], "normalize.enabled")
    num(n["target_lufs"], "normalize.target_lufs", -40, -5)
    num(n["true_peak_db"], "normalize.true_peak_db", -9, 0)
    num(n["loudness_range"], "normalize.loudness_range", 1, 20)


# --------------------------------------------------------------------------- stage: prepare
def pick_music_file(ctx: Context, s: dict, files: list[Path]) -> Path:
    wanted = s["file"]
    if wanted:
        for f in files:
            if wanted.lower() in (f.name.lower(), f.stem.lower()):
                return f
        raise ConfigError(f"Music file '{wanted}' not found in {ctx.paths['music']}. Available: {[f.name for f in files]}")
    return files[ctx.rng.randrange(len(files))]


def prepare(ctx: Context) -> None:
    log = ctx.logger
    s = load_feature_settings(ctx.paths["settings"], SETTINGS_FILE, DEFAULTS, ctx.config_warnings)
    validate_settings(s)
    for warning in ctx.config_warnings:
        log.warning(warning)
    ctx.config_warnings.clear()

    files = list_files(ctx.paths["music"], s["extensions"], "natural")
    if not files:
        raise ConfigError(
            f"No music files {s['extensions']} found in {ctx.paths['music']}. "
            "Add some, or set features.music to false in config.json."
        )
    src = pick_music_file(ctx, s, files)
    info = ff.probe_audio(ctx.ffprobe, src)
    if info is None:
        raise ConfigError(f"The music file could not be read: {src.name}")

    target = float(ctx.target_duration or clips.default_target(ctx))
    start = float(s["start_at_seconds"])
    need = target - start
    if need < 5:
        raise ConfigError(
            f"start_at_seconds ({start}) leaves only {need:.1f}s of video for the music "
            f"(video length {target:.1f}s)."
        )
    crossfade = float(s["loop"]["crossfade_seconds"])

    # Never convert more than the video can use (a 1-hour music file would waste minutes and disk).
    cap = need + crossfade + 10
    work = ctx.work_dir
    work.mkdir(parents=True, exist_ok=True)
    chain = ["aresample=48000"]
    chain.append("pan=stereo|c0=c0|c1=c0" if info.channels == 1 else "aformat=sample_fmts=fltp:channel_layouts=stereo")
    converted = work / "music_converted.wav"
    args = ["-i", str(src), "-vn", "-af", ",".join(chain)]
    if info.duration > cap:
        args += ["-t", f"{cap:.3f}"]
    args += ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(converted)]
    ff.run_capture(ctx.ffmpeg, args, label=f"convert music {src.name}")

    norm = work / "music_norm.wav"
    if s["normalize"]["enabled"]:
        log.info("Normalizing music to %g LUFS (reference level)", s["normalize"]["target_lufs"])
        loudnorm_file(ctx, converted, norm, s["normalize"], [], "music")
        converted.unlink(missing_ok=True)
    else:
        converted.replace(norm)

    used = ff.probe_audio(ctx.ffprobe, norm)
    if used is None:
        raise ConfigError(f"Could not read the converted music: {src.name}")
    ctx.state["music"] = {"name": src.name, "norm": norm, "duration": used.duration, "settings": s}
    log.info("Music: %s (%.1fs used of %.1fs) | %s", src.name, used.duration, info.duration,
             "random pick" if not s["file"] else "chosen in music.json")


# --------------------------------------------------------------------------- stage: mix
def duck_intervals(chunks: list[dict], attack: float, release: float, keep_gap: float) -> list[list[float]]:
    """Voiceover time ranges to duck under. Gaps shorter than `keep_gap` are bridged.

    The bridge threshold is never below attack + release, so two ducking ramps never overlap.
    """
    gap = max(keep_gap, attack + release)
    merged: list[list[float]] = []
    for c in chunks:
        a, b = float(c["start"]), float(c["end"])
        if merged and a - merged[-1][1] < gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return merged


def volume_expression(s: dict, intervals: list[list[float]]) -> str:
    """Linear gain over time: no_voice level, dipping to under_voice level around each interval."""
    idle = 10 ** (float(s["levels"]["no_voice_db"]) / 20)
    under = 10 ** (float(s["levels"]["under_voice_db"]) / 20)
    atk = float(s["ducking"]["attack_seconds"])
    rel = float(s["ducking"]["release_seconds"])
    terms = [
        f"clip(min((t-({a - atk:.3f}))/{atk:.3f},(({b + rel:.3f})-t)/{rel:.3f}),0,1)"
        for a, b in intervals
    ]
    return f"{idle:.5f}+({under - idle:.5f})*({'+'.join(terms)})"


def build_bed(ctx: Context, s: dict, info: dict, target: float, out: Path) -> int:
    """Render the finished music track (exactly `target` seconds). Returns the number of ducked sections."""
    start = float(s["start_at_seconds"])
    need = target - start
    length = float(info["duration"])
    crossfade = float(s["loop"]["crossfade_seconds"])
    if crossfade > 0 and length < 4 * crossfade:
        crossfade = length / 4
        ctx.logger.warning("Music is short; loop crossfade reduced to %.2fs.", crossfade)

    copies = 1 if length >= need else max(2, math.ceil((need - crossfade) / (length - crossfade)))
    if copies > MAX_LOOP_COPIES:
        raise ConfigError(
            f"The music ({length:.1f}s) is too short to loop over {need:.0f}s of video "
            f"({copies} copies). Use a longer music file."
        )

    args: list[str] = []
    parts: list[str] = []
    for i in range(copies):
        args += ["-i", str(info["norm"])]
        parts.append(
            f"[{i}:a:0]aformat=sample_fmts=fltp:channel_layouts=stereo:sample_rates=48000,"
            f"asetpts=PTS-STARTPTS[m{i}]"
        )
    if copies == 1:
        parts.append("[m0]anull[loop]")
    elif crossfade > 0:
        cur = "m0"
        for j in range(1, copies):
            nxt = "loop" if j == copies - 1 else f"x{j}"
            parts.append(f"[{cur}][m{j}]acrossfade=d={crossfade:.3f}:c1=qsin:c2=qsin[{nxt}]")
            cur = nxt
    else:
        parts.append("".join(f"[m{i}]" for i in range(copies)) + f"concat=n={copies}:v=0:a=1[loop]")

    post = [f"atrim=duration={need:.4f}", "asetpts=PTS-STARTPTS"]
    fade_in = min(float(s["fade"]["in_seconds"]), need / 3)
    fade_out = min(float(s["fade"]["out_seconds"]), need / 3)
    if fade_in > 0:
        post.append(f"afade=t=in:st=0:d={fade_in:.3f}:curve=qsin")
    if fade_out > 0:
        post.append(f"afade=t=out:st={need - fade_out:.3f}:d={fade_out:.3f}:curve=qsin")
    if start > 0:
        ms = int(round(start * 1000))
        post.append(f"adelay={ms}|{ms}")
    post += [f"apad=whole_dur={target:.4f}", f"atrim=duration={target:.4f}", "asetpts=PTS-STARTPTS"]

    voice = ctx.state.get("voiceover")
    intervals: list[list[float]] = []
    if s["ducking"]["enabled"] and voice:
        intervals = duck_intervals(
            voice["timeline"]["chunks"], float(s["ducking"]["attack_seconds"]),
            float(s["ducking"]["release_seconds"]), float(s["ducking"]["keep_ducked_gaps_shorter_than_seconds"]),
        )
    if intervals:
        # Small frames so the gain changes smoothly (about every 5 ms).
        post.append("asetnsamples=n=256:p=0")
        post.append(f"volume=volume='{volume_expression(s, intervals)}':eval=frame:precision=float")
    else:
        post.append(f"volume={float(s['levels']['no_voice_db']):g}dB")

    graph = ";".join(parts) + f";[loop]{','.join(post)}[bed]"
    if len(graph) > MAX_FILTER_CHARS:
        raise ConfigError("The music/ducking filter is too long for Windows. Increase ducking.keep_ducked_gaps_shorter_than_seconds.")

    ff.run_ffmpeg(
        ctx.ffmpeg,
        [*args, "-filter_complex", graph, "-map", "[bed]", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(out)],
        total_seconds=target, log_path=ctx.paths["logs"] / "ffmpeg_music_bed.log", label="music track",
        logger=ctx.logger, loglevel=str(ctx.cfg["ffmpeg"]["log_level"]),
    )
    loop_text = "no loop needed" if copies == 1 else f"looped {copies}x"
    ctx.logger.info("Music: %s, fade in %.1fs / out %.1fs, %d ducked section(s)",
                    loop_text, fade_in, fade_out, len(intervals))
    return len(intervals)


def mix(ctx: Context) -> Path | None:
    """Build the music bed and add it to the video's audio."""
    info = ctx.state.get("music")
    if ctx.dry_run or ctx.video is None or info is None:
        return None
    s = info["settings"]
    target = float(ctx.target_duration or 0)
    audio = ctx.cfg["audio"]
    bed = ctx.work_dir / "music_bed.wav"
    build_bed(ctx, s, info, target, bed)

    out = ctx.work_dir / "stage3_music.mp4"
    args = ["-i", str(ctx.video), "-i", str(bed)]
    if ff.probe_audio(ctx.ffprobe, ctx.video) is not None:
        # Existing audio (voiceover and/or clip audio) + music, added without any automatic level change.
        graph = (
            "[0:a:0]aformat=sample_fmts=fltp:channel_layouts=stereo:sample_rates=48000[ea];"
            "[1:a:0]aformat=sample_fmts=fltp:channel_layouts=stereo:sample_rates=48000[ma];"
            "[ea][ma]amerge=inputs=2,pan=stereo|c0=c0+c2|c1=c1+c3,alimiter=limit=0.97[aout]"
        )
        args += ["-filter_complex", graph, "-map", "0:v:0", "-map", "[aout]"]
        ctx.logger.info("Adding music to the existing audio")
    else:
        args += ["-map", "0:v:0", "-map", "1:a:0"]
        ctx.logger.info("Adding music (the video has no other audio)")
    args += ["-c:v", "copy", "-c:a", str(audio["codec"]), "-b:a", str(audio["bitrate"]),
             "-ar", str(audio["sample_rate"]), "-ac", "2", "-shortest", "-movflags", "+faststart", str(out)]
    ff.run_ffmpeg(
        ctx.ffmpeg, args, total_seconds=target, log_path=ctx.paths["logs"] / "ffmpeg_music_mix.log",
        label="mix music", logger=ctx.logger, loglevel=str(ctx.cfg["ffmpeg"]["log_level"]),
    )
    ctx.video = out
    return out
