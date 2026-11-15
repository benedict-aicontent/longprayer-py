"""Feature 2 - Voiceover.

Two stages (see pipeline.py):

  prepare(ctx)  runs BEFORE the clips. It builds one voiceover track
                  [lead silence] voice 1 [pause] voice 2 [pause] ... voice N [tail silence]
                and sets ctx.target_duration to its length, so the clip video is exactly as long
                as the voiceover (including lead and tail).
  mix(ctx)      runs AFTER the clips. It lays the voiceover track over the video
                (on top of the clips' own audio unless clips.json has tracks_muted = true).

Voice sources (settings/voiceover.json -> "source"):
  recorded  audio files in /voiceovers, in filename order (natural sort: 2 comes before 10)
  script    .txt file(s) in /script, split by blank lines, each paragraph spoken by Piper TTS.
            A blank line is a pause (pauses.between_seconds).
  auto      recorded if /voiceovers has audio files, otherwise script.

Speed, loudness and volume are applied the same way to both sources.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core import ffmpeg_tools as ff
from core.config_loader import choice, flag, load_feature_settings, num
from core.context import Context
from core.errors import ConfigError, VoiceoverError
from core.files import list_files
from core.loudness import loudnorm_file

SETTINGS_FILE = "voiceover.json"
MAX_FILTER_CHARS = 28000

DEFAULTS: dict = {
    "source": "auto",
    "speed": 1.0,
    "volume_db": 0.0,
    "pauses": {"between_seconds": 1.0, "lead_seconds": 5.0, "tail_seconds": 20.0},
    "recorded": {
        "extensions": [".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".opus"],
        "order": "natural",
    },
    "script": {"extensions": [".txt"], "multiply_pause_by_blank_lines": True},
    "piper": {
        "command": ["piper"],
        "model_flag": "-m",
        "output_flag": "-f",
        "text_mode": "argument",
        "voice": None,
        "extra_args": [],
        "use_cache": True,
        "timeout_seconds": 300,
    },
    "normalize": {
        "mode": "whole_track",
        "target_lufs": -16.0,
        "true_peak_db": -1.5,
        "loudness_range": 11.0,
    },
}


@dataclass
class Chunk:
    label: str            # file name, or "paragraph N"
    text: str | None      # script text (None for recorded files)
    wav: Path             # processed 48 kHz stereo wav (speed + optional per-file loudness applied)
    duration: float
    pause_after: float    # silence after this chunk (0 for the last one)
    start: float = 0.0    # position inside the final voiceover track
    end: float = 0.0


# --------------------------------------------------------------------------- settings
def validate_settings(s: dict) -> None:
    choice(s["source"], ("auto", "recorded", "script"), "source")
    num(s["speed"], "speed", 0.5, 2.0)
    num(s["volume_db"], "volume_db", -30, 30)
    for key in ("between_seconds", "lead_seconds", "tail_seconds"):
        num(s["pauses"][key], f"pauses.{key}", 0, 3600)
    for section in ("recorded", "script"):
        exts = s[section]["extensions"]
        if not isinstance(exts, list) or not exts:
            raise ConfigError(f"'{section}.extensions' must be a non-empty list like [\".mp3\"].")
    choice(s["recorded"]["order"], ("natural", "alphabetical"), "recorded.order")
    flag(s["script"]["multiply_pause_by_blank_lines"], "script.multiply_pause_by_blank_lines")

    p = s["piper"]
    if not isinstance(p["command"], list) or not p["command"] or not all(isinstance(c, str) for c in p["command"]):
        raise ConfigError('\'piper.command\' must be a list of strings, e.g. ["piper"] or ["py", "-3.12", "-m", "piper"].')
    if not isinstance(p["extra_args"], list):
        raise ConfigError("'piper.extra_args' must be a list of strings.")
    if p["voice"] is not None and not isinstance(p["voice"], str):
        raise ConfigError("'piper.voice' must be null or a voice file name such as \"en_US-lessac-medium\".")
    choice(p["text_mode"], ("argument", "stdin"), "piper.text_mode")
    flag(p["use_cache"], "piper.use_cache")
    num(p["timeout_seconds"], "piper.timeout_seconds", 10)

    n = s["normalize"]
    choice(n["mode"], ("whole_track", "per_file", "off"), "normalize.mode")
    num(n["target_lufs"], "normalize.target_lufs", -40, -5)
    num(n["true_peak_db"], "normalize.true_peak_db", -9, 0)
    num(n["loudness_range"], "normalize.loudness_range", 1, 20)


# --------------------------------------------------------------------------- file discovery
def parse_script(texts: list[str]) -> list[tuple[str, int]]:
    """Split script text into paragraphs.

    Returns [(paragraph_text, blank_lines_after)]. Lines inside a paragraph are joined with spaces.
    The end of a file counts as one blank line.
    """
    lines: list[str] = []
    for text in texts:
        lines += text.replace("\r\n", "\n").replace("\r", "\n").split("\n") + [""]

    paragraphs: list[list] = []
    current: list[str] = []
    for line in lines:
        if line.strip() == "":
            if current:
                paragraphs.append([" ".join(current), 0])
                current = []
            if paragraphs:
                paragraphs[-1][1] += 1
        else:
            current.append(re.sub(r"\s+", " ", line.strip()))
    if current:
        paragraphs.append([" ".join(current), 0])
    return [(text, blanks) for text, blanks in paragraphs if re.search(r"\w", text)]


# --------------------------------------------------------------------------- chunk processing
def convert_chunk(ctx: Context, s: dict, src: Path, dst: Path, channels: int, label: str) -> float:
    """Convert any audio to 48 kHz stereo wav, apply speed (and per-file loudness). Returns duration."""
    chain = ["aresample=48000"]
    # Mono -> stereo by copying the channel (the default mix would be 3 dB quieter).
    chain.append("pan=stereo|c0=c0|c1=c0" if channels == 1 else "aformat=sample_fmts=fltp:channel_layouts=stereo")
    speed = float(s["speed"])
    if abs(speed - 1.0) > 1e-6:
        chain.append(f"atempo={speed:.4f}")

    per_file = s["normalize"]["mode"] == "per_file"
    raw = dst.with_suffix(".pre.wav") if per_file else dst
    ff.run_capture(
        ctx.ffmpeg,
        ["-i", str(src), "-vn", "-af", ",".join(chain), "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(raw)],
        label=f"convert {label}",
    )
    if per_file:
        loudnorm_file(ctx, raw, dst, s["normalize"], [], label)
        raw.unlink(missing_ok=True)
    info = ff.probe_audio(ctx.ffprobe, dst)
    if info is None:
        raise VoiceoverError(f"Could not read the converted audio for {label}.")
    return info.duration


# --------------------------------------------------------------------------- source: recorded files
def build_recorded(ctx: Context, s: dict, files: list[Path], vo_dir: Path) -> list[Chunk]:
    between = float(s["pauses"]["between_seconds"])
    chunks: list[Chunk] = []
    for src in files:
        info = ff.probe_audio(ctx.ffprobe, src)
        if info is None:
            ctx.logger.warning("Skipped (unreadable or no audio): %s", src.name)
            continue
        wav = vo_dir / f"vo_{len(chunks) + 1:03d}.wav"
        duration = convert_chunk(ctx, s, src, wav, info.channels, src.name)
        chunks.append(Chunk(label=src.name, text=None, wav=wav, duration=duration, pause_after=between))
    if not chunks:
        raise VoiceoverError(f"None of the files in {ctx.paths['voiceovers']} could be read.")
    chunks[-1].pause_after = 0.0
    return chunks


# --------------------------------------------------------------------------- source: script + Piper
def resolve_voice_model(ctx: Context, s: dict) -> Path:
    voices_dir = ctx.paths["voices"]
    wanted = s["piper"]["voice"]
    available = sorted(voices_dir.glob("*.onnx")) if voices_dir.is_dir() else []
    if wanted:
        candidate = Path(wanted)
        options = [candidate] if candidate.is_absolute() else [
            voices_dir / wanted, voices_dir / f"{wanted}.onnx"
        ]
        for option in options:
            if option.is_file():
                return option
        names = [p.name for p in available] or "none found"
        raise ConfigError(
            f"Piper voice '{wanted}' not found in {voices_dir}. Voices available: {names}"
        )
    if not available:
        raise ConfigError(
            f"No Piper voice (.onnx file) found in {ctx.paths['voices']}. "
            "Put the voice .onnx and its .onnx.json there, or change paths.voices in config.json."
        )
    return available[0]


def run_piper(s: dict, model: Path, text: str, out_wav: Path) -> None:
    p = s["piper"]
    cmd = [*map(str, p["command"]), str(p["model_flag"]), str(model),
           str(p["output_flag"]), str(out_wav), *map(str, p["extra_args"])]
    if p["text_mode"] == "argument":
        # piper-tts 1.x (pip install piper-tts): the text is an argument after "--" ("--" protects text starting with "-")
        cmd += ["--", text]
        feed: dict = {"stdin": subprocess.DEVNULL}
    else:
        # the older piper.exe: the text is read from standard input
        feed = {"input": (text + "\n").encode("utf-8")}
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=float(p["timeout_seconds"]), **feed)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Could not start Piper ('{p['command'][0]}'). Set piper.command in settings/voiceover.json, "
            'e.g. ["C:/piper/piper.exe"] or ["py", "-3.12", "-m", "piper"].'
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise VoiceoverError(f"Piper timed out after {p['timeout_seconds']}s on: {text[:60]}...") from exc
    if result.returncode != 0 or not out_wav.is_file() or out_wav.stat().st_size < 64:
        tail = "\n".join(result.stderr.decode("utf-8", "replace").strip().splitlines()[-8:])
        raise VoiceoverError(
            f"Piper failed (exit code {result.returncode}) on: {text[:60]}...\n{tail}\n"
            f"Command: {subprocess.list2cmdline(cmd)}\n"
            "If your Piper uses different option names, change piper.model_flag / output_flag / text_mode "
            "(\"argument\" for piper-tts 1.x, \"stdin\" for the old piper.exe). "
            "Run  py -3.12 -m piper --help  to see what your version accepts."
        )


def build_script(ctx: Context, s: dict, script_files: list[Path], vo_dir: Path) -> list[Chunk]:
    p = s["piper"]
    if shutil.which(p["command"][0]) is None:
        raise ConfigError(
            f"Piper command '{p['command'][0]}' was not found. Set piper.command in "
            'settings/voiceover.json, e.g. ["C:/piper/piper.exe"] or ["py", "-3.12", "-m", "piper"].'
        )
    model = resolve_voice_model(ctx, s)
    if not model.with_name(model.name + ".json").is_file():
        ctx.logger.warning("No %s.json next to the voice model; Piper may refuse to run.", model.name)
    ctx.logger.info("Piper voice: %s", model.name)

    texts = [f.read_text(encoding="utf-8-sig", errors="replace") for f in script_files]
    paragraphs = parse_script(texts)
    if not paragraphs:
        raise VoiceoverError(f"The script in {ctx.paths['script']} has no readable text.")

    between = float(s["pauses"]["between_seconds"])
    multiply = bool(s["script"]["multiply_pause_by_blank_lines"])
    cache_dir = ctx.paths["temp"] / "tts_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    stat = model.stat()
    voice_id = f"{model.name}|{stat.st_size}|{stat.st_mtime_ns}|{p['extra_args']}"

    chunks: list[Chunk] = []
    for i, (text, blanks) in enumerate(paragraphs, 1):
        key = hashlib.sha1(f"{voice_id}|{text}".encode("utf-8")).hexdigest()[:24]
        cached = cache_dir / f"{key}.wav"
        if p["use_cache"] and cached.is_file() and cached.stat().st_size > 64:
            ctx.logger.debug("TTS cache hit for paragraph %d", i)
        else:
            print(f"\r    Piper: paragraph {i}/{len(paragraphs)}", end="", flush=True)
            run_piper(s, model, text, cached)
        info = ff.probe_audio(ctx.ffprobe, cached)
        if info is None:
            cached.unlink(missing_ok=True)
            raise VoiceoverError(f"Piper produced no audio for paragraph {i}: {text[:60]}...")
        wav = vo_dir / f"vo_{i:03d}.wav"
        duration = convert_chunk(ctx, s, cached, wav, info.channels, f"paragraph {i}")
        pause = between * (max(blanks, 1) if multiply else 1)
        chunks.append(Chunk(label=f"paragraph {i}", text=text, wav=wav, duration=duration, pause_after=pause))
    print()
    chunks[-1].pause_after = 0.0
    return chunks


# --------------------------------------------------------------------------- compile the track
def layout(chunks: list[Chunk], lead: float, tail: float) -> float:
    """Fill in start/end of every chunk. Returns the total length of the track."""
    t = lead
    for c in chunks:
        c.start, c.end = t, t + c.duration
        t = c.end + c.pause_after
    return t + tail


def compile_track(ctx: Context, chunks: list[Chunk], lead: float, tail: float, out: Path, total: float) -> None:
    items: list[tuple[str, object]] = []
    if lead > 0:
        items.append(("silence", lead))
    for c in chunks:
        items.append(("file", c.wav))
        if c.pause_after > 0:
            items.append(("silence", c.pause_after))
    if tail > 0:
        items.append(("silence", tail))

    args: list[str] = []
    parts: list[str] = []
    labels: list[str] = []
    file_index = 0
    for n, (kind, value) in enumerate(items):
        if kind == "file":
            args += ["-i", str(value)]
            parts.append(
                f"[{file_index}:a:0]aformat=sample_fmts=fltp:channel_layouts=stereo:sample_rates=48000,"
                f"asetpts=PTS-STARTPTS[p{n}]"
            )
            file_index += 1
        else:
            parts.append(
                f"anullsrc=r=48000:cl=stereo,atrim=duration={float(value):.4f},asetpts=PTS-STARTPTS[p{n}]"
            )
        labels.append(f"[p{n}]")
    parts.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[out]")
    graph = ";".join(parts)
    if len(graph) > MAX_FILTER_CHARS:
        raise VoiceoverError("The voiceover has too many parts for one FFmpeg command (Windows limit).")

    ff.run_ffmpeg(
        ctx.ffmpeg,
        [*args, "-filter_complex", graph, "-map", "[out]", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(out)],
        total_seconds=total, log_path=ctx.paths["logs"] / "ffmpeg_voiceover_compile.log",
        label="voiceover track", logger=ctx.logger, loglevel=str(ctx.cfg["ffmpeg"]["log_level"]),
    )


def finish_track(ctx: Context, s: dict, raw: Path, final: Path) -> None:
    """Whole-track loudness normalisation and/or volume gain."""
    gain = float(s["volume_db"])
    post = [f"volume={gain:g}dB", "alimiter=limit=0.97"] if abs(gain) > 1e-6 else []
    mode = s["normalize"]["mode"]
    if mode == "whole_track":
        ctx.logger.info("Normalizing loudness to %g LUFS", s["normalize"]["target_lufs"])
        loudnorm_file(ctx, raw, final, s["normalize"], post, "voiceover track")
    elif post:
        ff.run_capture(
            ctx.ffmpeg,
            ["-i", str(raw), "-af", ",".join(post), "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(final)],
            label="voiceover volume",
        )
    else:
        shutil.move(str(raw), str(final))


# --------------------------------------------------------------------------- stage: prepare
def prepare(ctx: Context) -> None:
    log = ctx.logger
    s = load_feature_settings(ctx.paths["settings"], SETTINGS_FILE, DEFAULTS, ctx.config_warnings)
    validate_settings(s)
    for warning in ctx.config_warnings:
        log.warning(warning)
    ctx.config_warnings.clear()

    work = ctx.work_dir
    vo_dir = work / "voiceover"
    vo_dir.mkdir(parents=True, exist_ok=True)

    recorded = list_files(ctx.paths["voiceovers"], s["recorded"]["extensions"], s["recorded"]["order"])
    scripts = list_files(ctx.paths["script"], s["script"]["extensions"], "natural")
    source = s["source"]
    if source == "auto":
        source = "recorded" if recorded else "script"
    if source == "recorded" and not recorded:
        raise ConfigError(f"source is 'recorded' but no audio files were found in {ctx.paths['voiceovers']}.")
    if source == "script" and not scripts:
        raise ConfigError(
            f"No voiceover audio in {ctx.paths['voiceovers']} and no script (.txt) in {ctx.paths['script']}. "
            "Add one of them, or set features.voiceover to false in config.json."
        )

    speed = float(s["speed"])
    log.info("Voiceover source: %s | speed %.2fx | loudness: %s",
             source, speed, s["normalize"]["mode"])
    if source == "recorded":
        log.info("%d recorded file(s) in %s", len(recorded), ctx.paths["voiceovers"])
        chunks = build_recorded(ctx, s, recorded, vo_dir)
    else:
        log.info("Script file(s): %s", ", ".join(f.name for f in scripts))
        chunks = build_script(ctx, s, scripts, vo_dir)

    lead = float(s["pauses"]["lead_seconds"])
    tail = float(s["pauses"]["tail_seconds"])
    total = layout(chunks, lead, tail)

    raw = work / "voiceover_raw.wav"
    final = work / "voiceover_track.wav"
    compile_track(ctx, chunks, lead, tail, raw, total)
    finish_track(ctx, s, raw, final)

    if ctx.target_duration:
        log.warning("--duration is ignored: the video length follows the voiceover.")
    ctx.target_duration = total
    timeline = {
        "source": source, "speed": speed, "lead_seconds": lead, "tail_seconds": tail,
        "total_seconds": round(total, 3),
        "chunks": [
            {"index": i, "label": c.label, "text": c.text,
             "start": round(c.start, 3), "end": round(c.end, 3)}
            for i, c in enumerate(chunks, 1)
        ],
    }
    (work / "voiceover_timeline.json").write_text(json.dumps(timeline, indent=2, ensure_ascii=False), encoding="utf-8")
    ctx.state["voiceover"] = {
        "track": final, "timeline": timeline, "volume_db": float(s["volume_db"]),
        "source": source, "chunk_wavs": [c.wav for c in chunks],
    }

    for i, c in enumerate(chunks, 1):
        log.info("  %3d. %-28s %7.2fs -> %7.2fs  (%.2fs)", i, c.label[:28], c.start, c.end, c.duration)
    voice_len = chunks[-1].end - chunks[0].start
    log.info("Voiceover: %.1fs of speech incl. pauses | lead %.1fs | tail %.1fs | VIDEO LENGTH %.1fs (%d:%04.1f)",
             voice_len, lead, tail, total, int(total // 60), total % 60)


# --------------------------------------------------------------------------- stage: mix
def mix(ctx: Context) -> Path | None:
    """Lay the voiceover track over the combined video."""
    info = ctx.state.get("voiceover")
    if ctx.dry_run or ctx.video is None or info is None:
        return None
    audio = ctx.cfg["audio"]
    keep_clip_audio = bool(ctx.state.get("clips_keep_audio", False))
    # When background music is mixed next, keep the audio lossless (mkv + PCM) so it is encoded
    # to AAC only once, at the very end.
    music_follows = bool(ctx.cfg["features"].get("music", False))
    out = ctx.work_dir / ("stage2_voiceover.mkv" if music_follows else "stage2_voiceover.mp4")

    args = ["-i", str(ctx.video), "-i", str(info["track"])]
    if keep_clip_audio:
        # amerge + pan adds the two sources without any automatic level change.
        graph = (
            "[0:a:0]aformat=sample_fmts=fltp:channel_layouts=stereo:sample_rates=48000[ca];"
            "[1:a:0]aformat=sample_fmts=fltp:channel_layouts=stereo:sample_rates=48000[va];"
            "[ca][va]amerge=inputs=2,pan=stereo|c0=c0+c2|c1=c1+c3,alimiter=limit=0.97[aout]"
        )
        args += ["-filter_complex", graph, "-map", "0:v:0", "-map", "[aout]"]
    else:
        args += ["-map", "0:v:0", "-map", "1:a:0"]
    if music_follows:
        args += ["-c:v", "copy", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", "-shortest", str(out)]
    else:
        args += ["-c:v", "copy", "-c:a", str(audio["codec"]), "-b:a", str(audio["bitrate"]),
                 "-ar", str(audio["sample_rate"]), "-ac", "2", "-shortest", "-movflags", "+faststart", str(out)]

    ctx.logger.info("Adding voiceover to the video (%s)",
                    "mixed with clip audio" if keep_clip_audio else "clip audio is muted")
    ff.run_ffmpeg(
        ctx.ffmpeg, args, total_seconds=float(ctx.target_duration or 0),
        log_path=ctx.paths["logs"] / "ffmpeg_voiceover_mix.log", label="mix voiceover",
        logger=ctx.logger, loglevel=str(ctx.cfg["ffmpeg"]["log_level"]),
    )
    ctx.video = out
    return out
