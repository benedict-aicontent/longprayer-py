"""Speech recognition with faster-whisper (word-level timestamps), with a disk cache.

faster-whisper is imported only when it is needed, so the rest of the program (and the
`source: "script"` caption mode) works without it installed.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.context import Context
from core.errors import CaptionError, ConfigError

INSTALL_HINT = "py -3.12 -m pip install faster-whisper"


@dataclass
class Word:
    text: str
    start: float
    end: float


def _file_hash(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _decode_audio(ctx: Context, wav: Path):
    """Decode `wav` to 16 kHz mono float32 with our own FFmpeg.

    faster-whisper normally decodes file paths itself with PyAV ("av"), and some av versions
    break it (error: open() got an unexpected keyword argument 'metadata_errors'). Giving it
    a ready-made array means PyAV is never used.
    """
    import numpy as np

    cmd = [ctx.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(wav),
           "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"]
    try:
        done = subprocess.run(cmd, capture_output=True, check=False)
    except OSError as exc:
        raise CaptionError(f"Could not run FFmpeg to read {wav.name}: {exc}") from exc
    if done.returncode != 0 or not done.stdout:
        detail = done.stderr.decode("utf-8", "replace").strip()[-500:]
        raise CaptionError(f"FFmpeg could not decode {wav.name} for speech recognition: {detail}")
    return np.frombuffer(done.stdout, dtype="<f4").copy()


def _load_model(ctx: Context, w: dict):
    cached = ctx.state.get("_whisper_model")
    if cached is not None:
        return cached
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ConfigError(
            "faster-whisper is not installed, but captions need it (settings/caption.json: source = "
            f"\"faster_whisper\"). Install it with:  {INSTALL_HINT}\n"
            "Or set source to \"script\" if your voiceover comes from the script folder."
        ) from exc

    ctx.logger.info(
        "Loading Whisper model '%s' (%s, %s). The first run downloads the model, so it needs internet.",
        w["model"], w["device"], w["compute_type"],
    )
    kwargs = {"device": str(w["device"]), "compute_type": str(w["compute_type"])}
    if int(w["cpu_threads"]) > 0:
        kwargs["cpu_threads"] = int(w["cpu_threads"])
    if w["model_dir"]:
        kwargs["download_root"] = str(w["model_dir"])
    try:
        model = WhisperModel(str(w["model"]), **kwargs)
    except Exception as exc:
        raise CaptionError(
            f"Could not load the Whisper model '{w['model']}': {exc}\n"
            "If this is the first run, check your internet connection (the model is downloaded once)."
        ) from exc
    ctx.state["_whisper_model"] = model
    return model


def transcribe_words(ctx: Context, wav: Path, w: dict, label: str) -> list[Word]:
    """Words with start/end seconds, relative to the start of `wav`."""
    cache_dir = ctx.paths["temp"] / "caption_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(
        f"{_file_hash(wav)}|{w['model']}|{w['language']}|{w['beam_size']}|{w['vad_filter']}|{w['compute_type']}".encode()
    ).hexdigest()[:24]
    cache_file = cache_dir / f"{key}.json"
    if cache_file.is_file():
        try:
            rows = json.loads(cache_file.read_text(encoding="utf-8"))
            ctx.logger.debug("Transcript cache hit for %s", label)
            return [Word(r["t"], float(r["s"]), float(r["e"])) for r in rows]
        except (ValueError, KeyError, OSError):
            cache_file.unlink(missing_ok=True)

    model = _load_model(ctx, w)
    language = None if str(w["language"]).lower() in ("auto", "") else str(w["language"])
    words: list[Word] = []
    try:
        audio = _decode_audio(ctx, wav)
        segments, info = model.transcribe(
            audio, language=language, beam_size=int(w["beam_size"]), word_timestamps=True,
            vad_filter=bool(w["vad_filter"]), condition_on_previous_text=False,
        )
        total = float(getattr(info, "duration", 0) or 0)
        for seg in segments:
            if total > 0:
                print(f"\r    transcribing {label}: {min(100, seg.end / total * 100):5.1f}%", end="", flush=True)
            seg_words = getattr(seg, "words", None) or []
            if seg_words:
                for item in seg_words:
                    text = str(item.word).strip()
                    if text:
                        words.append(Word(text, float(item.start), float(item.end)))
            else:  # no word timestamps for this segment: spread its text evenly
                parts = str(seg.text).split()
                span = max(0.05, float(seg.end) - float(seg.start))
                for i, part in enumerate(parts):
                    words.append(Word(part, float(seg.start) + span * i / len(parts),
                                      float(seg.start) + span * (i + 1) / len(parts)))
        print(f"\r    transcribing {label}: 100.0%")
    except (ConfigError, CaptionError):
        raise
    except Exception as exc:
        print()
        raise CaptionError(f"Speech recognition failed for {label}: {exc}") from exc

    cache_file.write_text(
        json.dumps([{"t": x.text, "s": x.start, "e": x.end} for x in words], ensure_ascii=False),
        encoding="utf-8",
    )
    return words
