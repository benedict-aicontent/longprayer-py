"""Small file-discovery helpers shared by the features."""
from __future__ import annotations

import re
from pathlib import Path


def natural_key(name: str) -> list:
    """Sort key so that 2_x comes before 10_x."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def list_files(folder: Path, extensions: list[str], order: str = "natural") -> list[Path]:
    """Files in `folder` (not sub-folders) with one of `extensions`, sorted by name."""
    if not folder.is_dir():
        return []
    wanted = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in wanted]
    key = (lambda p: natural_key(p.name)) if order == "natural" else (lambda p: p.name.lower())
    return sorted(files, key=key)
