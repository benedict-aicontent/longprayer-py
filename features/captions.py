"""Feature 4 - Auto-captions.

Stages (see pipeline.py):

  prepare(ctx)  runs BEFORE the long render, right after the voiceover is built:
                1. get every spoken word with its time
                     source "faster_whisper": speech recognition on each voiceover file
                       (when the voiceover came from the script folder and use_script_text is true, the
                        SCRIPT's words are shown and the recognition only supplies the timing)
                     source "script": the script's words spread over each file's speech (no recognition)
                2. group the words into caption lines and write auto-caption.txt for corrections
                   (or read your corrected auto-caption.txt when edit_file.mode is "use_edited")
                3. write an ASS subtitle file and register it, so the clip render burns it in
  preview(...)  renders a short test video (captions over your first clip) for tuning the style fast.

Captions appear only while the voice speaks. All style settings are in settings/caption.json.
"""
from __future__ import annotations

import random
import re
from pathlib import Path

from core import ass
from core import ffmpeg_tools as ff
from core.config_loader import choice, flag, load_feature_settings, num
from core.context import Context
from core.errors import CaptionError, ConfigError
from core.files import list_files
from core.fonts import missing_fonts
from core.transcribe import Word, transcribe_words
from features import caption_pages as cp
from features import caption_style as cs

SETTINGS_FILE = "caption.json"
VIDEO_EXTENSIONS = [".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"]

_NONE_ROLE = {k: None for k in cs.ROLE_KEYS}
DEFAULTS: dict = {
    "source": "faster_whisper",
    "use_script_text": True,
    "check_ffmpeg_filter": True,
    "whisper": {
        "model": "small", "device": "cpu", "compute_type": "int8", "language": "en",
        "beam_size": 5, "vad_filter": False, "cpu_threads": 0, "model_dir": None,
    },
    "edit_file": {"path": "auto-caption.txt", "mode": "generate", "keep_previous": True, "time_decimals": 1},
    "page": {
        "max_words": 8, "max_chars": 56, "max_gap_seconds": 0.8,
        "break_after_sentence": True, "prefer_breaks_at_commas": True, "merge_short_sentences_up_to_words": 2,
    },
    "timing": {"lead_in_seconds": 0.08, "hold_after_seconds": 0.25, "min_page_seconds": 0.6, "time_offset_seconds": 0.0},
    "position": {"bottom_margin_px": 120, "y_offset_px": 0, "side_margin_px": 160},
    "styles": {
        "base": {
            "font": "Segoe UI", "size": 88, "bold": True, "italic": False, "color": "#FFFFFF",
            "opacity": 1.0, "text_transform": "none", "letter_spacing_px": 0, "outline_px": 4,
            "outline_color": "#000000", "shadow_px": 2, "shadow_color": "#000000", "shadow_opacity": 0.5,
        },
        "small": {**_NONE_ROLE, "font": "Georgia", "size": 62, "bold": False, "italic": True},
        "keyword": {**_NONE_ROLE, "font": "Segoe UI Black", "size": 98, "bold": False,
                    "italic": True, "text_transform": "upper"},
    },
    "mixed_fonts": {
        "enabled": True,
        "apply_to": ["small_words", "keywords"],
        "small_words": ["a", "an", "the", "to", "of", "in", "on", "at", "by", "for", "from", "with",
                        "and", "or", "but", "as", "is", "are", "was", "were", "be", "it", "its",
                        "this", "that", "my", "me", "i", "we", "you", "your", "our", "us"],
        "keywords": ["god", "jesus", "christ", "lord", "father", "holy", "spirit", "amen",
                     "faith", "grace", "peace", "love", "hope", "mercy", "strength", "blessing"],
    },
    "highlight": {"enabled": True, "color": "#4DE3FF", "after_spoken": "base", "dim_opacity": 0.6,
                  "transition_ms": 70, "bridge_gaps_under_seconds": 0.3},
    "glow": {"enabled": True, "color": "#4DE3FF", "size_px": 8, "blur": 14, "opacity": 0.9,
             "apply_to": ["active_word"]},
    "animation": {"mode": "random_per_line", "fixed": "pop",
                  "pool": ["fade", "pop", "bounce", "slide_up", "drop", "blur_in"], "in_ms": 220, "out_ms": 150},
}


# --------------------------------------------------------------------------- settings
def _check_role(r: dict, name: str, inherit: bool) -> None:
    def present(key: str) -> bool:
        return not (inherit and r[key] is None)

    if present("font") and (not isinstance(r["font"], str) or not r["font"].strip()):
        raise ConfigError(f"'styles.{name}.font' must be a font name such as \"Segoe UI\".")
    if present("size"):
        num(r["size"], f"styles.{name}.size", 8, 600)
    for key in ("bold", "italic"):
        if present(key):
            flag(r[key], f"styles.{name}.{key}")
    for key in ("color", "outline_color", "shadow_color"):
        if present(key):
            ass.parse_hex_color(r[key], f"styles.{name}.{key}")
    for key in ("opacity", "shadow_opacity"):
        if present(key):
            num(r[key], f"styles.{name}.{key}", 0, 1)
    if present("text_transform"):
        choice(r["text_transform"], ("none", "upper", "lower", "title"), f"styles.{name}.text_transform")
    if present("letter_spacing_px"):
        num(r["letter_spacing_px"], f"styles.{name}.letter_spacing_px", -20, 60)
    for key in ("outline_px", "shadow_px"):
        if present(key):
            num(r[key], f"styles.{name}.{key}", 0, 40)


def _subset(values, allowed: tuple[str, ...], name: str) -> None:
    if not isinstance(values, list) or any(v not in allowed for v in values):
        raise ConfigError(f"'{name}' must be a list containing only {list(allowed)} (got {values!r}).")


def validate_settings(s: dict) -> None:
    choice(s["source"], ("faster_whisper", "script"), "source")
    flag(s["use_script_text"], "use_script_text")
    flag(s["check_ffmpeg_filter"], "check_ffmpeg_filter")
    w = s["whisper"]
    for key in ("model", "compute_type", "language"):
        if not isinstance(w[key], str) or not w[key].strip():
            raise ConfigError(f"'whisper.{key}' must be a text value (got {w[key]!r}).")
    choice(w["device"], ("cpu", "cuda", "auto"), "whisper.device")
    num(w["beam_size"], "whisper.beam_size", 1, 10)
    flag(w["vad_filter"], "whisper.vad_filter")
    num(w["cpu_threads"], "whisper.cpu_threads", 0, 64)
    if w["model_dir"] is not None and not isinstance(w["model_dir"], str):
        raise ConfigError("'whisper.model_dir' must be null or a folder path.")

    e = s["edit_file"]
    if not isinstance(e["path"], str) or not e["path"].strip():
        raise ConfigError("'edit_file.path' must be a file name such as \"auto-caption.txt\".")
    choice(e["mode"], ("generate", "use_edited"), "edit_file.mode")
    flag(e["keep_previous"], "edit_file.keep_previous")
    if int(num(e["time_decimals"], "edit_file.time_decimals", 0, 2)) != e["time_decimals"]:
        raise ConfigError("'edit_file.time_decimals' must be 0, 1 or 2.")

    p = s["page"]
    num(p["max_words"], "page.max_words", 1, 30)
    num(p["max_chars"], "page.max_chars", 10, 200)
    num(p["max_gap_seconds"], "page.max_gap_seconds", 0.1, 30)
    flag(p["break_after_sentence"], "page.break_after_sentence")
    flag(p["prefer_breaks_at_commas"], "page.prefer_breaks_at_commas")
    num(p["merge_short_sentences_up_to_words"], "page.merge_short_sentences_up_to_words", 0, 10)

    t = s["timing"]
    num(t["lead_in_seconds"], "timing.lead_in_seconds", 0, 5)
    num(t["hold_after_seconds"], "timing.hold_after_seconds", 0, 10)
    num(t["min_page_seconds"], "timing.min_page_seconds", 0.1, 30)
    num(t["time_offset_seconds"], "timing.time_offset_seconds", -10, 10)

    pos = s["position"]
    num(pos["bottom_margin_px"], "position.bottom_margin_px", 0, 2000)
    num(pos["y_offset_px"], "position.y_offset_px", -2000, 2000)
    num(pos["side_margin_px"], "position.side_margin_px", 0, 900)

    _check_role(s["styles"]["base"], "base", inherit=False)
    _check_role(s["styles"]["small"], "small", inherit=True)
    _check_role(s["styles"]["keyword"], "keyword", inherit=True)

    m = s["mixed_fonts"]
    flag(m["enabled"], "mixed_fonts.enabled")
    _subset(m["apply_to"], ("small_words", "keywords"), "mixed_fonts.apply_to")
    for key in ("small_words", "keywords"):
        if not isinstance(m[key], list) or not all(isinstance(x, str) for x in m[key]):
            raise ConfigError(f"'mixed_fonts.{key}' must be a list of words.")

    h = s["highlight"]
    flag(h["enabled"], "highlight.enabled")
    ass.parse_hex_color(h["color"], "highlight.color")
    choice(h["after_spoken"], ("base", "keep", "dim"), "highlight.after_spoken")
    num(h["dim_opacity"], "highlight.dim_opacity", 0, 1)
    num(h["transition_ms"], "highlight.transition_ms", 0, 1000)
    num(h["bridge_gaps_under_seconds"], "highlight.bridge_gaps_under_seconds", 0, 5)

    g = s["glow"]
    flag(g["enabled"], "glow.enabled")
    ass.parse_hex_color(g["color"], "glow.color")
    num(g["size_px"], "glow.size_px", 0, 60)
    num(g["blur"], "glow.blur", 0, 60)
    num(g["opacity"], "glow.opacity", 0, 1)
    _subset(g["apply_to"], ("active_word", "keywords", "all"), "glow.apply_to")

    a = s["animation"]
    choice(a["mode"], ("random_per_line", "random_once", "fixed"), "animation.mode")
    choice(a["fixed"], cs.ANIMATIONS, "animation.fixed")
    if not isinstance(a["pool"], list) or not a["pool"]:
        raise ConfigError("'animation.pool' must be a non-empty list of animation names.")
    _subset(a["pool"], cs.ANIMATIONS, "animation.pool")
    num(a["in_ms"], "animation.in_ms", 50, 3000)
    num(a["out_ms"], "animation.out_ms", 0, 3000)


def load_settings(ctx: Context) -> dict:
    s = load_feature_settings(ctx.paths["settings"], SETTINGS_FILE, DEFAULTS, ctx.config_warnings)
    validate_settings(s)
    for warning in ctx.config_warnings:
        ctx.logger.warning(warning)
    ctx.config_warnings.clear()
    return s


# --------------------------------------------------------------------------- words per voiceover file
def speech_span(ctx: Context, wav: Path, duration: float) -> tuple[float, float]:
    """Where the speech starts and ends inside a voiceover file (skips silence at both ends)."""
    try:
        report = ff.run_capture(
            ctx.ffmpeg, ["-i", str(wav), "-af", "silencedetect=noise=-42dB:d=0.12", "-f", "null", "-"],
            label="find speech", loglevel="info",
        )
    except Exception:
        return 0.0, duration
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", report)]
    ends = [float(x) for x in re.findall(r"silence_end: (-?[\d.]+)", report)]
    t0, t1 = 0.0, duration
    if starts and ends and starts[0] <= 0.05:
        t0 = ends[0]
    if len(starts) > len(ends):
        t1 = starts[-1]
    elif starts and ends and ends[-1] >= duration - 0.05:
        t1 = starts[-1]
    if t1 - t0 < 0.3:
        return 0.0, duration
    return max(0.0, t0 - 0.02), min(duration, t1 + 0.02)


def collect_groups(ctx: Context, s: dict, vo: dict) -> list[list[Word]]:
    """Words (on the video timeline) for every voiceover file."""
    log = ctx.logger
    from_script = vo["source"] == "script"
    if s["source"] == "script" and not from_script:
        raise ConfigError(
            "caption source 'script' needs the voiceover to come from the script folder (Piper). "
            "Your voiceover comes from recorded files, so set source to \"faster_whisper\" in settings/caption.json."
        )
    use_script = from_script and (s["source"] == "script" or s["use_script_text"])
    log.info("Caption words: %s", "script text, timed by speech recognition" if use_script and s["source"] != "script"
             else "script text spread over each paragraph's speech" if s["source"] == "script"
             else "speech recognition (faster-whisper)")

    groups: list[list[Word]] = []
    chunks, wavs = vo["timeline"]["chunks"], vo["chunk_wavs"]
    for number, (chunk, wav) in enumerate(zip(chunks, wavs), 1):
        offset = float(chunk["start"])
        duration = float(chunk["end"]) - offset
        label = f"{number}/{len(chunks)} ({chunk['label']})"
        script_words = chunk["text"].split() if use_script and chunk.get("text") else []

        if s["source"] == "script":
            t0, t1 = speech_span(ctx, wav, duration)
            words = cp.distribute(script_words, offset + t0, offset + t1)
        else:
            heard = transcribe_words(ctx, wav, s["whisper"], label)
            if script_words and heard:
                aligned = cp.align_script_to_asr(script_words, heard, (0.0, duration))
                words = [Word(w.text, offset + w.start, offset + w.end) for w in aligned]
            elif script_words:
                log.warning("Nothing was recognised in %s; spreading the script text over its speech instead.", label)
                t0, t1 = speech_span(ctx, wav, duration)
                words = cp.distribute(script_words, offset + t0, offset + t1)
            else:
                words = [Word(w.text, offset + w.start, offset + w.end) for w in heard]
                if not words:
                    log.warning("Nothing was recognised in %s - no captions for it.", label)
        if words:
            groups.append(words)
    return groups


def build_pages_for_video(ctx: Context, s: dict, vo: dict) -> list[cp.Page]:
    log = ctx.logger
    e = s["edit_file"]
    txt = Path(e["path"]).expanduser()
    txt = txt if txt.is_absolute() else ctx.root / txt
    fp = cp.fingerprint(vo["timeline"]["chunks"])

    if e["mode"] == "use_edited":
        pages = cp.read_edit_file(txt, fp, log)
        log.info("Captions: %d lines read from your corrected %s", len(pages), txt.name)
    else:
        groups = collect_groups(ctx, s, vo)
        pages = cp.build_pages(groups, s["page"])
        if not pages:
            raise CaptionError("No words were found for the captions. Is the voiceover silent?")
        cp.write_edit_files(txt, pages, fp, bool(e["keep_previous"]), int(e["time_decimals"]))
        log.info("Captions: %d lines. To correct spelling/timing, edit %s, then set edit_file.mode to "
                 "\"use_edited\" in settings/caption.json.", len(pages), txt)
    cp.set_display_times(pages, s["timing"])
    return pages


# --------------------------------------------------------------------------- ass file
def require_ass_filter(ctx: Context, s: dict, *, what: str = "captions", feature: str = "captions",
                     settings_file: str = "caption.json") -> None:
    """Burning captions (or on-screen text) in needs FFmpeg's 'ass' filter (libass). Fail early with a clear message."""
    if not s["check_ffmpeg_filter"]:
        ctx.logger.debug("FFmpeg 'ass' filter check skipped (check_ffmpeg_filter is false).")
        return
    if ff.has_filter(ctx.ffmpeg, "ass") is False:
        raise ConfigError(
            f"FFmpeg ({ctx.ffmpeg}) reports that it has no 'ass' filter (libass), which {what} need. "
            "Use a full FFmpeg build (for example the 'full' build from gyan.dev) and set ffmpeg.ffmpeg_path "
            f"in config.json to it, or set features.{feature} to false. You can check with:  "
            'ffmpeg -hide_banner -h filter=ass   (it must print "Filter ass", not "Unknown filter"). '
            f'If you are sure your FFmpeg has it, set "check_ffmpeg_filter" to false in settings/{settings_file}.'
        )


def write_ass(ctx: Context, s: dict, pages: list[cp.Page], *, name: str = "captions.ass",
              time_shift: float = 0.0, limit: float | None = None) -> Path:
    width, height = int(ctx.cfg["video"]["width"]), int(ctx.cfg["video"]["height"])
    rng = random.Random(ctx.seed ^ 0xCA97)  # separate generator: captions never change the clip choices
    events, used = cs.build_events(pages, s, width, height, rng, time_shift=time_shift, limit=limit)
    if not events:
        raise CaptionError("There are no captions in the selected time range.")
    base = s["styles"]["base"]
    text = ass.header(width, height, side_margin=int(s["position"]["side_margin_px"]),
                      font=str(base["font"]), size=int(base["size"])) + "\n".join(events) + "\n"
    ctx.work_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.work_dir / name
    path.write_text(text, encoding="utf-8")
    ctx.logger.info("Caption animations: %s",
                    ", ".join(f"{k} x{v}" for k, v in sorted(used.items())) or "none")

    roles = cs.resolve_roles(s["styles"])
    wanted = [roles["base"]["font"]]
    if s["mixed_fonts"]["enabled"]:
        if "small_words" in s["mixed_fonts"]["apply_to"]:
            wanted.append(roles["small"]["font"])
        if "keywords" in s["mixed_fonts"]["apply_to"]:
            wanted.append(roles["keyword"]["font"])
    gone = missing_fonts(wanted)
    if gone:
        ctx.logger.warning(
            "Font not found on this PC: %s. Windows will substitute another font. Run  py -3.12 main.py "
            "--list-fonts  to see the exact names you can use in settings/caption.json.", ", ".join(gone))
    return path


# --------------------------------------------------------------------------- stage: prepare
def prepare(ctx: Context) -> None:
    s = load_settings(ctx)
    vo = ctx.state.get("voiceover")
    if vo is None:
        raise ConfigError(
            "Captions need the voiceover feature (they follow the spoken words). "
            "Set features.voiceover to true in config.json, or set features.captions to false."
        )
    if ctx.dry_run:
        ctx.logger.info("Captions: skipped in a dry run (use --caption-preview to test them).")
        return
    require_ass_filter(ctx, s)
    pages = build_pages_for_video(ctx, s, vo)
    path = write_ass(ctx, s, pages)
    ctx.state.setdefault("video_post_filters", []).append(f"ass={path.name}")


# --------------------------------------------------------------------------- preview
def preview(ctx: Context, start: float | None, length: float) -> Path:
    """Render a short test video: your first clip (looped) + the voiceover + the captions."""
    s = load_settings(ctx)
    vo = ctx.state.get("voiceover")
    if vo is None:
        raise ConfigError("--caption-preview needs the voiceover feature (features.voiceover = true).")
    require_ass_filter(ctx, s)
    pages = build_pages_for_video(ctx, s, vo)
    if start is None:
        start = max(0.0, pages[0].start - 1.0)
    path = write_ass(ctx, s, pages, name="captions_preview.ass", time_shift=start, limit=length)

    width, height, fps = int(ctx.cfg["video"]["width"]), int(ctx.cfg["video"]["height"]), float(ctx.cfg["video"]["fps"])
    clips = list_files(ctx.paths["clips"], VIDEO_EXTENSIONS)
    if clips:
        source = ["-stream_loop", "-1", "-i", str(clips[0])]
        chain = (f"fps={fps:g},scale={width}:{height}:force_original_aspect_ratio=increase,"
                 f"crop={width}:{height},setsar=1,format=yuv420p")
        ctx.logger.info("Preview background: %s", clips[0].name)
    else:
        source = ["-f", "lavfi", "-i", f"color=c=0x24303c:s={width}x{height}:r={fps:g}"]
        chain = "format=yuv420p"
    out = ctx.paths["output"] / "caption_preview.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [*source, "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(vo["track"]),
            "-vf", f"{chain},ass={path.name}", "-map", "0:v:0", "-map", "1:a:0", "-t", f"{length:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    ff.run_ffmpeg(ctx.ffmpeg, args, total_seconds=length, log_path=ctx.paths["logs"] / "ffmpeg_caption_preview.log",
                  label="caption preview", logger=ctx.logger, loglevel=str(ctx.cfg["ffmpeg"]["log_level"]),
                  cwd=ctx.work_dir)
    return out
