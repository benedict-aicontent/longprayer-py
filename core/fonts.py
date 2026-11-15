"""Which fonts are installed? Used to warn about a misspelled or missing font name.

Windows: reads the font registry (the same list Windows uses). Linux (only used while developing):
asks fontconfig. Other systems: no check (returns None).
"""
from __future__ import annotations

import re
import subprocess
import sys

_STYLE_WORDS = {"italic", "bold", "regular", "oblique"}
_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")


def _expand(name: str) -> set[str]:
    """'Segoe UI Black Italic (TrueType)' -> {'Segoe UI Black Italic', 'Segoe UI Black'}.

    Style words at the end are peeled off one by one, so both the full name and the family name
    can be matched. 'Black' / 'Light' / 'Semibold' are NOT peeled: 'Segoe UI Black' is a family.
    """
    found: set[str] = set()
    for part in _SUFFIX.sub("", name).split(" & "):
        words = part.strip().split()
        while words:
            found.add(" ".join(words))
            if words[-1].lower() in _STYLE_WORDS and len(words) > 1:
                words = words[:-1]
            else:
                break
    return found


def _windows_names() -> set[str]:
    import winreg  # only exists on Windows

    names: set[str] = set()
    sub_key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, sub_key)
        except OSError:
            continue
        with key:
            index = 0
            while True:
                try:
                    value_name = winreg.EnumValue(key, index)[0]
                except OSError:
                    break
                index += 1
                names |= _expand(value_name)
    return names


def _fontconfig_names() -> set[str]:
    try:
        out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    names: set[str] = set()
    for line in out.splitlines():
        for family in line.split(","):
            if family.strip():
                names.add(family.strip().replace("\\-", "-"))
    return names


def installed_font_names() -> set[str] | None:
    """Installed font / family names, or None when this system cannot be checked."""
    try:
        if sys.platform.startswith("win"):
            return _windows_names()
        if sys.platform.startswith("linux"):
            return _fontconfig_names() or None
    except Exception:  # a font check must never stop a video from being made
        return None
    return None


def missing_fonts(wanted: list[str]) -> list[str]:
    """Names from `wanted` that are not installed (empty if everything is found or unknown)."""
    names = installed_font_names()
    if not names:
        return []
    have = {n.lower() for n in names}
    return sorted({f for f in wanted if f.lower() not in have})
