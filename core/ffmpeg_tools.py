"""Thin, safe wrappers around ffmpeg / ffprobe (no shell, Windows friendly)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from core.errors import ConfigError, FfmpegError


@dataclass
class MediaInfo:
    path: Path
    duration: float
    width: int
    height: int
    has_audio: bool


def find_binary(configured: str, label: str) -> str:
    """Resolve 'ffmpeg' / a full path to an executable, or raise a helpful error."""
    found = shutil.which(configured)
    if not found:
        raise ConfigError(
            f"{label} not found ('{configured}'). Install FFmpeg and add it to PATH, "
            f"or set the full path in config.json (ffmpeg section)."
        )
    return found


def probe(ffprobe: str, path: Path) -> MediaInfo | None:
    """Return media info, or None if the file is unreadable / has no video stream."""
    cmd = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    streams = data.get("streams", [])
    video = [
        s for s in streams
        if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic", 0) != 1
    ]
    if not video:
        return None
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    duration = None
    for candidate in (data.get("format", {}).get("duration"), video[0].get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue
    if not duration or duration <= 0:
        return None
    return MediaInfo(
        path=path,
        duration=duration,
        width=int(video[0].get("width", 0)),
        height=int(video[0].get("height", 0)),
        has_audio=has_audio,
    )


def list_xfade_transitions(ffmpeg: str) -> set[str]:
    """Names of xfade transitions supported by the installed FFmpeg (empty set if unknown)."""
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-h", "filter=xfade"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    names = set(re.findall(r"^\s+([a-z0-9]+)\s+-?\d+\s+\.\.FV", result.stdout, flags=re.M))
    names.discard("custom")
    return names


def run_ffmpeg(
    ffmpeg: str,
    args: list[str],
    *,
    total_seconds: float,
    log_path: Path,
    label: str,
    logger,
    loglevel: str = "error",
    cwd: Path | None = None,
) -> None:
    """Run ffmpeg with a live progress line. stderr is saved to `log_path`.

    `cwd` is the folder ffmpeg runs in (filters that read a file, like 'ass=captions.ass', resolve it there).
    """
    cmd = [ffmpeg, "-hide_banner", "-y", "-loglevel", loglevel, "-nostats",
           "-progress", "pipe:1", *args]
    logger.debug("ffmpeg command: %s", subprocess.list2cmdline(cmd))
    log_path.parent.mkdir(parents=True, exist_ok=True)

    last_print = 0.0
    with open(log_path, "w", encoding="utf-8") as err_file:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=err_file,
            text=True, encoding="utf-8", errors="replace",
            cwd=str(cwd) if cwd else None,
        )
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and value.lstrip("-").isdigit() and total_seconds > 0:
                    now = time.monotonic()
                    if now - last_print >= 1.0:
                        pct = max(0.0, min(100.0, int(value) / 1_000_000 / total_seconds * 100))
                        print(f"\r    {label}: {pct:5.1f}%", end="", flush=True)
                        last_print = now
            return_code = proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            print()
            raise

    if return_code != 0:
        print()
        tail = ""
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(lines[-15:])
        except OSError:
            pass
        raise FfmpegError(
            f"FFmpeg failed during '{label}' (exit code {return_code}).\n{tail}\n"
            f"Full FFmpeg log: {log_path}"
        )
    print(f"\r    {label}: 100.0%")


# --------------------------------------------------------------------------- audio helpers
@dataclass
class AudioInfo:
    path: Path
    duration: float
    channels: int


def probe_audio(ffprobe: str, path: Path) -> AudioInfo | None:
    """Return duration/channels of the first audio stream, or None if unreadable / no audio."""
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        return None
    duration = None
    for candidate in (data.get("format", {}).get("duration"), audio[0].get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue
    if not duration or duration <= 0:
        return None
    return AudioInfo(path=path, duration=duration, channels=int(audio[0].get("channels", 2) or 2))


# One row of `ffmpeg -filters`:  " T.C ass     V->V   Render ASS subtitles ..."  (FFmpeg 6)
#                                " .. ass      V->V   Render ASS subtitles ..."  (FFmpeg 9: 2 flag characters)
# The number of flag characters has changed between FFmpeg versions, so accept 2 to 4 of them.
_FILTER_ROW = re.compile(r"^\s*[A-Z.]{2,4}\s+(\S+)\s+\S+->\S+\s", re.M)
_MIN_ROWS_TO_TRUST_LISTING = 50  # a listing we could not parse well must never be used to say "missing"


def filter_names(listing: str) -> set[str]:
    """Filter names found in the output of `ffmpeg -filters`."""
    return {m.group(1) for m in _FILTER_ROW.finditer(listing)}


def _run_text(cmd: list[str]) -> str | None:
    """stdout + stderr of a short command, or None if it could not be run."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (result.stdout or "") + "\n" + (result.stderr or "")


def has_filter(ffmpeg: str, name: str) -> bool | None:
    """True/False: does this FFmpeg build have the filter? None if that could not be determined.

    First ask FFmpeg about this one filter (`ffmpeg -h filter=NAME` answers "Filter NAME" or
    "Unknown filter 'NAME'" on every version). If that answer is not recognised, fall back to the
    full `ffmpeg -filters` list. "Missing" is only reported when FFmpeg said so, or when the list
    was parsed well (many rows) and the filter is not in it; otherwise the answer is None (unknown).
    """
    answer = _run_text([ffmpeg, "-hide_banner", "-h", f"filter={name}"])
    if answer:
        if re.search(rf"^\s*Filter {re.escape(name)}\b", answer, flags=re.M):
            return True
        if re.search(r"unknown filter", answer, flags=re.I):
            return False
    listing = _run_text([ffmpeg, "-hide_banner", "-filters"])
    if not listing:
        return None
    names = filter_names(listing)
    if name in names:
        return True
    return False if len(names) >= _MIN_ROWS_TO_TRUST_LISTING else None


def run_capture(
    ffmpeg: str, args: list[str], *, label: str, loglevel: str = "error", timeout: float | None = None
) -> str:
    """Run a short ffmpeg job (no progress display). Returns stderr text."""
    cmd = [ffmpeg, "-hide_banner", "-y", "-nostats", "-loglevel", loglevel, *args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(f"FFmpeg timed out during '{label}'.") from exc
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-15:])
        raise FfmpegError(f"FFmpeg failed during '{label}' (exit code {result.returncode}).\n{tail}")
    return result.stderr
