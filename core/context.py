"""Shared run context passed to every feature (stage)."""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Context:
    root: Path
    cfg: dict
    paths: dict[str, Path]
    logger: logging.Logger
    rng: random.Random
    seed: int
    ffmpeg: str
    ffprobe: str
    work_dir: Path
    dry_run: bool = False
    config_warnings: list[str] = field(default_factory=list)

    # Length of the final video in seconds. Feature 1 reads it from settings/clips.json
    # (or --duration). Feature 2 (voiceover) will overwrite it with the real voiceover length.
    target_duration: float | None = None

    # The video produced by the most recent stage. Each stage reads this and writes a new file.
    video: Path | None = None

    # Free-form values stages can share with later stages.
    state: dict[str, Any] = field(default_factory=dict)
