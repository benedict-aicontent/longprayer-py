"""Feature 1 - Combine video clips into one long 16:9 video.

Flow:
  1. Find and probe every clip in the clips folder.
  2. PLAN: pick random trims (min/max seconds), looping through the clips until the
     target duration is covered. One transition type is chosen for the whole video.
  3. RENDER: join the planned segments with FFmpeg 'xfade' (video) and 'acrossfade' (audio).
     Long plans are rendered in batches (join.batch_size) to keep RAM low on small PCs,
     then the batches are joined again in the final pass.

All behaviour is controlled by settings/clips.json (+ video/audio sections of config.json).
"""
from __future__ import annotations

import math
import random
import shutil
from dataclasses import dataclass
from pathlib import Path

from core import ffmpeg_tools as ff
from core.config_loader import choice, flag, load_feature_settings, num
from core.context import Context
from core.errors import ConfigError

SETTINGS_FILE = "clips.json"
MAX_FILTER_CHARS = 28000  # Windows command lines are limited to ~32k characters

DEFAULTS: dict = {
    "target_duration_seconds": 180,
    "extensions": [".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"],
    "order": "random",
    "when_exhausted": "reshuffle",
    "avoid_back_to_back_repeat": True,
    "clip_duration": {"min_seconds": 4.0, "max_seconds": 8.0},
    "trim_position": "random",
    "short_clip_policy": "use_whole",
    "fit_mode": "fill",
    "tracks_muted": False,
    "clip_audio_volume": 1.0,
    "transition": {
        "enabled": True,
        "mode": "random_once",
        "fixed_type": "fade",
        "duration_seconds": 0.8,
        "random_pool": [
            "fade", "dissolve", "fadeblack", "fadewhite", "smoothleft", "smoothright",
            "wipeleft", "wiperight", "slideleft", "slideright", "circleopen", "radial",
        ],
    },
    "join": {"batch_size": 8, "intermediate_preset": "superfast", "intermediate_crf": 18},
}


@dataclass
class Segment:
    path: Path
    start: float
    duration: float
    has_audio: bool


# --------------------------------------------------------------------------- settings
def validate_settings(s: dict) -> None:
    num(s["target_duration_seconds"], "target_duration_seconds", 1)
    if not isinstance(s["extensions"], list) or not s["extensions"]:
        raise ConfigError("'extensions' must be a non-empty list such as [\".mp4\", \".mov\"].")
    choice(s["order"], ("random", "alphabetical"), "order")
    choice(s["when_exhausted"], ("reshuffle", "repeat_in_order"), "when_exhausted")
    flag(s["avoid_back_to_back_repeat"], "avoid_back_to_back_repeat")
    choice(s["trim_position"], ("random", "start"), "trim_position")
    choice(s["short_clip_policy"], ("use_whole", "skip"), "short_clip_policy")
    choice(s["fit_mode"], ("fill", "fit"), "fit_mode")
    flag(s["tracks_muted"], "tracks_muted")
    num(s["clip_audio_volume"], "clip_audio_volume", 0, 10)

    mn = num(s["clip_duration"]["min_seconds"], "clip_duration.min_seconds", 0.5)
    num(s["clip_duration"]["max_seconds"], "clip_duration.max_seconds", mn)

    tr = s["transition"]
    flag(tr["enabled"], "transition.enabled")
    choice(tr["mode"], ("random_once", "fixed"), "transition.mode")
    if tr["enabled"]:
        t = num(tr["duration_seconds"], "transition.duration_seconds", 0.1, 5)
        if mn < 2 * t + 0.2:
            raise ConfigError(
                f"clip_duration.min_seconds ({mn}) must be at least "
                f"2 x transition.duration_seconds + 0.2 = {2 * t + 0.2:.2f}. "
                "Raise min_seconds or shorten the transition."
            )
    j = s["join"]
    if int(num(j["batch_size"], "join.batch_size", 2)) != j["batch_size"]:
        raise ConfigError("'join.batch_size' must be a whole number.")
    num(j["intermediate_crf"], "join.intermediate_crf", 0, 51)


def choose_transition(ctx: Context, s: dict) -> str | None:
    """One transition type for the whole video (or None for hard cuts)."""
    tr = s["transition"]
    if not tr["enabled"]:
        return None
    supported = ff.list_xfade_transitions(ctx.ffmpeg)

    def usable(name: str) -> bool:
        return not supported or name in supported

    if tr["mode"] == "fixed":
        name = str(tr["fixed_type"])
        if not usable(name):
            raise ConfigError(
                f"transition.fixed_type '{name}' is not supported by your FFmpeg. "
                f"Supported: {sorted(supported)}"
            )
        return name

    pool = [str(n) for n in tr["random_pool"] if usable(str(n))]
    dropped = [str(n) for n in tr["random_pool"] if not usable(str(n))]
    if dropped:
        ctx.logger.warning("Transitions not supported by your FFmpeg were skipped: %s", dropped)
    if not pool:
        raise ConfigError(
            "transition.random_pool has no usable transitions. "
            f"Supported by your FFmpeg: {sorted(supported)}"
        )
    return ctx.rng.choice(pool)


# --------------------------------------------------------------------------- discovery
def collect_clips(clips_dir: Path, extensions: list[str]) -> list[Path]:
    if not clips_dir.is_dir():
        raise ConfigError(f"Clips folder not found: {clips_dir}. Create it and add your 16:9 clips.")
    exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}
    files = sorted(
        (p for p in clips_dir.iterdir() if p.is_file() and p.suffix.lower() in exts),
        key=lambda p: p.name.lower(),
    )
    if not files:
        raise ConfigError(f"No video clips found in {clips_dir} (looked for {sorted(exts)}).")
    return files


def probe_clips(ctx: Context, files: list[Path], floor: float) -> list[ff.MediaInfo]:
    usable: list[ff.MediaInfo] = []
    for path in files:
        info = ff.probe(ctx.ffprobe, path)
        if info is None:
            ctx.logger.warning("Skipped (unreadable or no video stream): %s", path.name)
        elif info.duration - 0.05 < floor:
            ctx.logger.warning(
                "Skipped (too short, %.2fs; need at least %.2fs): %s", info.duration, floor, path.name
            )
        else:
            usable.append(info)
    if not usable:
        raise ConfigError("None of the clips are usable. See the warnings above.")
    return usable


# --------------------------------------------------------------------------- planning
def _snap_down(seconds: float, fps: float) -> float:
    return math.floor(seconds * fps + 1e-9) / fps


def _snap_up(seconds: float, fps: float) -> float:
    return math.ceil(seconds * fps - 1e-9) / fps


def plan_segments(
    infos: list[ff.MediaInfo], s: dict, fps: float, t: float, target: float, rng: random.Random
) -> list[Segment]:
    """Choose random trims until the joined length reaches `target` seconds.

    Joined length with n segments = sum(durations) - (n - 1) * t   (t = transition length).
    The last segment is sized so the video ends right after its transition completes.
    """
    mn = float(s["clip_duration"]["min_seconds"])
    mx = float(s["clip_duration"]["max_seconds"])
    floor_d = _snap_up((2 * t + 0.2) if t > 0 else 0.5, fps)
    random_trim = s["trim_position"] == "random"
    reshuffle = s["order"] == "random" and s["when_exhausted"] == "reshuffle"

    base_order = list(infos)  # already alphabetical
    if s["order"] == "random":
        rng.shuffle(base_order)

    segments: list[Segment] = []
    total = 0.0
    previous: Path | None = None

    for pass_no in range(100000):
        order = list(base_order)
        if pass_no > 0 and reshuffle:
            rng.shuffle(order)
        if (
            pass_no > 0
            and s["avoid_back_to_back_repeat"]
            and len(order) > 1
            and order[0].path == previous
        ):
            order[0], order[1] = order[1], order[0]

        added = False
        for info in order:
            available = info.duration - 0.05
            wanted = rng.uniform(mn, mx)
            if wanted > available:
                if s["short_clip_policy"] == "skip":
                    continue
                wanted = available
            duration = max(_snap_down(wanted, fps), floor_d)
            if duration > available:
                continue
            start = round(rng.uniform(0, available - duration), 3) if random_trim else 0.0

            if not segments:
                if duration >= target:  # extremely short target: one clip is enough
                    duration = min(_snap_up(target, fps), _snap_down(available, fps))
                    segments.append(Segment(info.path, start, duration, info.has_audio))
                    return segments
                total = duration
            elif total + duration - t >= target - 1e-6:
                # Final segment: just long enough to finish its transition + the remaining time.
                needed = _snap_up(target - total + t + 0.05, fps)
                duration = min(needed, _snap_down(available, fps))
                segments.append(Segment(info.path, start, duration, info.has_audio))
                return segments
            else:
                total += duration - t

            segments.append(Segment(info.path, start, duration, info.has_audio))
            previous = info.path
            added = True

        if not added:
            raise ConfigError("Could not plan any segment. Check clip_duration and short_clip_policy.")
    raise ConfigError("Planning did not finish (internal limit reached).")


def expected_length(durations: list[float], t: float) -> float:
    return sum(durations) - (len(durations) - 1) * t


def print_plan(ctx: Context, segments: list[Segment], t: float, transition: str | None) -> None:
    log = ctx.logger
    log.info("Planned %d segments from %d clip file(s):",
             len(segments), len({seg.path for seg in segments}))
    for i, seg in enumerate(segments, 1):
        log.info("  %3d. %-40s start %7.2fs  length %5.2fs", i, seg.path.name[:40], seg.start, seg.duration)
    total = expected_length([seg.duration for seg in segments], t)
    log.info("Transition: %s (%.2fs)  |  planned length: %.2fs",
             transition or "none (hard cuts)", t, total)


# --------------------------------------------------------------------------- rendering
def build_filter_graph(
    segments: list[Segment], *, width: int, height: int, fps: float, fit_mode: str,
    transition: str | None, t: float, keep_audio: bool, volume: float, sample_rate: int,
    normalize: bool,
) -> tuple[str, float]:
    """Build the -filter_complex string. Returns (graph, expected output duration)."""
    parts: list[str] = []
    for i, seg in enumerate(segments):
        d = seg.duration
        chain = [f"fps={fps:g}"]
        if normalize:
            if fit_mode == "fill":
                chain.append(
                    f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
                )
            else:
                chain.append(
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
                )
        chain += ["setsar=1", "format=yuv420p", f"trim=duration={d:.4f}", "setpts=PTS-STARTPTS"]
        parts.append(f"[{i}:v:0]{','.join(chain)}[v{i}]")

        if keep_audio:
            if seg.has_audio:
                a_chain = [
                    f"aresample={sample_rate}",
                    "aformat=sample_fmts=fltp:channel_layouts=stereo",
                ]
                if abs(volume - 1.0) > 1e-6:
                    a_chain.append(f"volume={volume:g}")
                a_chain += ["apad", f"atrim=duration={d:.4f}", "asetpts=PTS-STARTPTS"]
                parts.append(f"[{i}:a:0]{','.join(a_chain)}[a{i}]")
            else:  # clip has no sound: generate silence so audio stays in sync
                parts.append(
                    f"anullsrc=r={sample_rate}:cl=stereo,atrim=duration={d:.4f},"
                    f"asetpts=PTS-STARTPTS[a{i}]"
                )

    n = len(segments)
    durations = [seg.duration for seg in segments]
    if n == 1:
        parts.append("[v0]null[vout]")
        if keep_audio:
            parts.append("[a0]anull[aout]")
        return ";".join(parts), durations[0]

    if transition is None:
        if keep_audio:
            inputs = "".join(f"[v{i}][a{i}]" for i in range(n))
            parts.append(f"{inputs}concat=n={n}:v=1:a=1[vout][aout]")
        else:
            inputs = "".join(f"[v{i}]" for i in range(n))
            parts.append(f"{inputs}concat=n={n}:v=1:a=0[vout]")
        return ";".join(parts), sum(durations)

    cum = durations[0]
    cur_v, cur_a = "v0", "a0"
    for j in range(1, n):
        last = j == n - 1
        out_v = "vout" if last else f"xv{j}"
        out_a = "aout" if last else f"xa{j}"
        parts.append(
            f"[{cur_v}][v{j}]xfade=transition={transition}:duration={t:.4f}:offset={cum - t:.4f}[{out_v}]"
        )
        if keep_audio:
            parts.append(f"[{cur_a}][a{j}]acrossfade=d={t:.4f}:c1=tri:c2=tri[{out_a}]")
        cur_v, cur_a = out_v, out_a
        cum += durations[j] - t
    return ";".join(parts), cum


def render_group(
    ctx: Context, segments: list[Segment], out_path: Path, *, s: dict, t: float,
    transition: str | None, intermediate: bool, normalize: bool, label: str,
    cut_at: float | None = None,
) -> float:
    """Render one FFmpeg job. Returns the expected duration of the output."""
    video, audio = ctx.cfg["video"], ctx.cfg["audio"]
    keep_audio = not s["tracks_muted"]
    graph, expected = build_filter_graph(
        segments,
        width=int(video["width"]), height=int(video["height"]), fps=float(video["fps"]),
        fit_mode=s["fit_mode"], transition=transition, t=t, keep_audio=keep_audio,
        volume=float(s["clip_audio_volume"]) if normalize else 1.0,
        sample_rate=int(audio["sample_rate"]), normalize=normalize,
    )
    if len(graph) > MAX_FILTER_CHARS:
        raise ConfigError(
            f"This batch is too large for Windows ({len(graph)} filter characters). "
            "Lower join.batch_size in settings/clips.json (8 to 12 is a good range)."
        )

    # Filters other features registered (captions) are applied only in the FINAL encode, on the finished timeline.
    post = [] if intermediate else list(ctx.state.get("video_post_filters", []))
    final_label = "vout"
    if post:
        graph += f";[vout]{','.join(post)}[vfinal]"
        final_label = "vfinal"
        if len(graph) > MAX_FILTER_CHARS:
            raise ConfigError("The final filter is too long for Windows. Lower join.batch_size in settings/clips.json.")

    args: list[str] = []
    for seg in segments:
        args += ["-ss", f"{seg.start:.3f}", "-t", f"{seg.duration + 0.5:.3f}", "-i", str(seg.path)]
    args += ["-filter_complex", graph, "-map", f"[{final_label}]"]
    args += ["-map", "[aout]"] if keep_audio else ["-an"]

    fps = f"{float(video['fps']):g}"
    if intermediate:
        j = s["join"]
        args += ["-c:v", "libx264", "-preset", str(j["intermediate_preset"]),
                 "-crf", str(j["intermediate_crf"]), "-pix_fmt", "yuv420p", "-r", fps]
        if keep_audio:
            args += ["-c:a", "pcm_s16le", "-ar", str(audio["sample_rate"])]
    else:
        args += ["-c:v", str(video["codec"]), "-preset", str(video["preset"]),
                 "-crf", str(video["crf"]), "-profile:v", str(video["profile"]),
                 "-pix_fmt", str(video["pixel_format"]), "-r", fps]
        if keep_audio:
            args += ["-c:a", str(audio["codec"]), "-b:a", str(audio["bitrate"]),
                     "-ar", str(audio["sample_rate"]), "-ac", "2"]
        args += ["-movflags", "+faststart"]
        if cut_at:
            args += ["-t", f"{cut_at:.3f}"]

    threads = int(ctx.cfg["ffmpeg"]["threads"])
    if threads > 0:
        args += ["-threads", str(threads)]
    args.append(str(out_path))

    log_path = ctx.paths["logs"] / f"ffmpeg_{out_path.stem}.log"
    ff.run_ffmpeg(
        ctx.ffmpeg, args, total_seconds=expected, log_path=log_path, label=label,
        logger=ctx.logger, loglevel=str(ctx.cfg["ffmpeg"]["log_level"]),
        cwd=ctx.work_dir if post else None,
    )
    return expected


def _balanced_chunks(items: list[Segment], batch: int) -> list[list[Segment]]:
    """Split into the fewest groups of at most `batch`, as even in size as possible."""
    groups = math.ceil(len(items) / batch)
    size, extra = divmod(len(items), groups)
    chunks, pos = [], 0
    for g in range(groups):
        end = pos + size + (1 if g < extra else 0)
        chunks.append(items[pos:end])
        pos = end
    return chunks


def join_segments(
    ctx: Context, segments: list[Segment], s: dict, t: float, transition: str | None,
    target: float, final_path: Path,
) -> None:
    fps = float(ctx.cfg["video"]["fps"])
    keep_audio = not s["tracks_muted"]
    batch = int(s["join"]["batch_size"])
    current = segments
    level = 0
    previous_parts: list[Path] = []

    while len(current) > batch:
        chunks = _balanced_chunks(current, batch)
        ctx.logger.info("Pass %d: rendering %d batches (up to %d clips each)", level + 1, len(chunks), batch)
        next_level: list[Segment] = []
        new_parts: list[Path] = []
        for index, chunk in enumerate(chunks, 1):
            part = ctx.work_dir / f"part_L{level}_{index:03d}.mkv"
            expected = render_group(
                ctx, chunk, part, s=s, t=t, transition=transition, intermediate=True,
                normalize=(level == 0), label=f"batch {index}/{len(chunks)}",
            )
            measured = ff.probe(ctx.ffprobe, part)
            # Durations and the transition length are whole frames, so `expected` is frame-exact.
            # Use the shorter of expected/measured so the next offset never lands past the end.
            usable = min(expected, measured.duration) if measured else expected
            next_level.append(Segment(part, 0.0, _snap_down(usable, fps), keep_audio))
            new_parts.append(part)
        if not ctx.cfg["general"]["keep_temp_files"]:
            for old in previous_parts:
                old.unlink(missing_ok=True)
        previous_parts = new_parts
        current = next_level
        level += 1

    ctx.logger.info("Final pass: joining %d item(s) and encoding %dx%d H264",
                    len(current), int(ctx.cfg["video"]["width"]), int(ctx.cfg["video"]["height"]))
    render_group(
        ctx, current, final_path, s=s, t=t, transition=transition, intermediate=False,
        normalize=(level == 0), label="final encode", cut_at=target,
    )
    if not ctx.cfg["general"]["keep_temp_files"]:
        for old in previous_parts:
            old.unlink(missing_ok=True)


# --------------------------------------------------------------------------- entry point
def default_target(ctx: Context) -> float:
    """Target length from settings/clips.json (used when no voiceover sets the length)."""
    s = load_feature_settings(ctx.paths["settings"], SETTINGS_FILE, DEFAULTS, [])
    return float(num(s["target_duration_seconds"], "target_duration_seconds", 1))


def run(ctx: Context) -> Path | None:
    """Stage entry point. Sets ctx.video to the combined video (None in dry-run)."""
    log = ctx.logger
    s = load_feature_settings(ctx.paths["settings"], SETTINGS_FILE, DEFAULTS, ctx.config_warnings)
    validate_settings(s)
    for warning in ctx.config_warnings:
        log.warning(warning)
    ctx.config_warnings.clear()

    fps = float(ctx.cfg["video"]["fps"])
    transition = choose_transition(ctx, s)
    t = float(s["transition"]["duration_seconds"]) if transition else 0.0
    if t > 0:  # whole frames keep every transition offset frame-exact (no drift when batching)
        t = max(1, round(t * fps)) / fps
    target = float(ctx.target_duration or s["target_duration_seconds"])
    ctx.target_duration = target
    floor = (2 * t + 0.2) if t > 0 else 0.5

    files = collect_clips(ctx.paths["clips"], s["extensions"])
    log.info("Found %d clip file(s) in %s", len(files), ctx.paths["clips"])
    infos = probe_clips(ctx, files, floor)
    if any(i.width * 9 != i.height * 16 for i in infos):
        log.info("Note: some clips are not exactly 16:9; fit_mode='%s' will be applied.", s["fit_mode"])

    log.info("Target length: %.1fs | seed: %d | audio from clips: %s",
             target, ctx.seed, "muted" if s["tracks_muted"] else "kept")
    segments = plan_segments(infos, s, fps, t, target, ctx.rng)
    print_plan(ctx, segments, t, transition)

    if ctx.dry_run:
        log.info("Dry run: nothing rendered.")
        return None

    ctx.work_dir.mkdir(parents=True, exist_ok=True)
    out = ctx.work_dir / "stage1_clips.mp4"
    join_segments(ctx, segments, s, t, transition, target, out)
    ctx.video = out
    ctx.state["clips_transition"] = transition
    ctx.state["clips_keep_audio"] = not s["tracks_muted"]
    return out
