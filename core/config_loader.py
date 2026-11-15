"""Load and validate config.json and per-feature settings files.

Rules:
* Missing keys fall back to the defaults defined in code.
* Unknown keys produce a warning (catches typos) but never crash.
* Keys that start with "_" are ignored, so you can leave notes in the JSON files.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from core.errors import ConfigError

DEFAULT_CONFIG: dict[str, Any] = {
    "general": {"random_seed": None, "keep_temp_files": False, "log_level": "INFO"},
    "features": {"voiceover": True, "music": True, "captions": True, "onscreen_text": True, "clips": True},
    "paths": {
        "clips": "clips",
        "voiceovers": "voiceovers",
        "voices": "voices",
        "script": "script",
        "music": "music",
        "output": "output",
        "temp": "temp",
        "logs": "logs",
        "settings": "settings",
    },
    "ffmpeg": {
        "ffmpeg_path": "ffmpeg",
        "ffprobe_path": "ffprobe",
        "threads": 0,
        "log_level": "error",
    },
    "video": {
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "codec": "libx264",
        "preset": "veryfast",
        "crf": 20,
        "profile": "high",
        "pixel_format": "yuv420p",
    },
    "audio": {"codec": "aac", "bitrate": "192k", "sample_rate": 48000},
    "output": {"name_prefix": "video_", "digits": 3},
}


# --------------------------------------------------------------------------- JSON
def load_json(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"Settings file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path.name} is not valid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}. "
            "Tip: on Windows paths use forward slashes (B:/folder) or double backslashes (B:\\\\folder)."
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a JSON object at the top level.")
    return data


def deep_merge(defaults: dict, override: dict, warnings: list[str], where: str = "") -> dict:
    """Return defaults overlaid with override. Unknown keys are reported in `warnings`."""
    result = copy.deepcopy(defaults)
    for key, value in override.items():
        if key.startswith("_"):
            continue
        label = f"{where}{key}"
        if key not in defaults:
            warnings.append(f"Unknown setting '{label}' (ignored). Check the spelling.")
            continue
        if isinstance(defaults[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(defaults[key], value, warnings, f"{label}.")
        else:
            result[key] = value
    return result


# --------------------------------------------------------------------------- loaders
def load_config(config_path: Path) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    raw = load_json(config_path)
    cfg = deep_merge(DEFAULT_CONFIG, raw, warnings)
    validate_config(cfg)
    return cfg, warnings


def load_feature_settings(
    settings_dir: Path, filename: str, defaults: dict, warnings: list[str] | None = None
) -> dict:
    """Load settings/<filename>, merged over `defaults`."""
    sink = warnings if warnings is not None else []
    raw = load_json(settings_dir / filename)
    return deep_merge(defaults, raw, sink, f"{filename}: ")


def resolve_paths(root: Path, cfg: dict) -> dict[str, Path]:
    """Turn the 'paths' section into absolute Paths (relative ones are relative to the project root)."""
    resolved: dict[str, Path] = {}
    for name, value in cfg["paths"].items():
        path = Path(str(value)).expanduser()
        resolved[name] = path if path.is_absolute() else (root / path)
    return resolved


# --------------------------------------------------------------------------- validators
def num(value: Any, name: str, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"'{name}' must be a number (got {value!r}).")
    if minimum is not None and value < minimum:
        raise ConfigError(f"'{name}' must be at least {minimum} (got {value}).")
    if maximum is not None and value > maximum:
        raise ConfigError(f"'{name}' must be at most {maximum} (got {value}).")
    return float(value)


def choice(value: Any, options: tuple[str, ...], name: str) -> str:
    if value not in options:
        raise ConfigError(f"'{name}' must be one of {list(options)} (got {value!r}).")
    return value


def flag(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"'{name}' must be true or false (got {value!r}).")
    return value


def validate_config(cfg: dict) -> None:
    video = cfg["video"]
    num(video["width"], "video.width", 320)
    num(video["height"], "video.height", 240)
    num(video["fps"], "video.fps", 1, 120)
    num(video["crf"], "video.crf", 0, 51)
    if int(video["width"]) % 2 or int(video["height"]) % 2:
        raise ConfigError("video.width and video.height must be even numbers for H264.")
    num(cfg["audio"]["sample_rate"], "audio.sample_rate", 8000)
    num(cfg["ffmpeg"]["threads"], "ffmpeg.threads", 0)
    num(cfg["output"]["digits"], "output.digits", 1, 8)
    choice(
        str(cfg["general"]["log_level"]).upper(),
        ("DEBUG", "INFO", "WARNING", "ERROR"),
        "general.log_level",
    )
    flag(cfg["general"]["keep_temp_files"], "general.keep_temp_files")
