"""Helpers for writing ASS subtitle files, which FFmpeg's libass ('ass' filter) burns onto the video.

Shared by the caption feature (Feature 4) and, later, the on-screen text feature (Feature 5).
"""
from __future__ import annotations

import re

from core.errors import ConfigError

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")

STYLE_FORMAT = (
    "Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
    "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
    "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
)
EVENT_FORMAT = "Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"


def parse_hex_color(value, name: str = "color") -> tuple[int, int, int]:
    """'#RRGGBB' -> (r, g, b). Raises a friendly ConfigError for anything else."""
    match = _HEX.match(value.strip()) if isinstance(value, str) else None
    if not match:
        raise ConfigError(f"'{name}' must be a color like \"#FFFFFF\" (got {value!r}).")
    h = match.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def color(value, name: str = "color") -> str:
    """'#RRGGBB' -> ASS colour '&HBBGGRR&'."""
    r, g, b = parse_hex_color(value, name)
    return f"&H{b:02X}{g:02X}{r:02X}&"


def alpha(opacity: float) -> str:
    """Opacity 1.0 (solid) .. 0.0 (invisible) -> ASS alpha '&H00&' .. '&HFF&'."""
    o = min(1.0, max(0.0, float(opacity)))
    return f"&H{int(round((1.0 - o) * 255)):02X}&"


def timestamp(seconds: float) -> str:
    """Seconds -> ASS time 'H:MM:SS.cc'."""
    cs = max(0, int(round(float(seconds) * 100)))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def escape_text(text: str) -> str:
    """Remove characters that have a special meaning inside ASS text."""
    text = text.replace("\\", "/").replace("{", "(").replace("}", ")")
    return re.sub(r"[\r\n]+", " ", text)


def header(width: int, height: int, *, side_margin: int = 0, font: str = "Arial",
           size: int = 60, title: str = "TheFaithFuel", extra_styles: tuple[str, ...] = ()) -> str:
    """Script info + one 'Default' style + the start of the events section.

    Everything visible (fonts, colours, sizes) is set per word with override tags, so the single
    Default style only supplies the side margins that decide where lines wrap.
    """
    style = (
        f"Style: Default,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        f"0,0,0,0,100,100,0,0,1,0,0,2,{int(side_margin)},{int(side_margin)},0,1"
    )
    return "\n".join([
        "[Script Info]",
        f"Title: {title}",
        "ScriptType: v4.00+",
        f"PlayResX: {int(width)}",
        f"PlayResY: {int(height)}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        f"Format: {STYLE_FORMAT}",
        style,
        *extra_styles,
        "",
        "[Events]",
        f"Format: {EVENT_FORMAT}",
        "",
    ])


def box_style(name: str, side_margin: int = 0) -> str:
    """A style whose outline is drawn as a filled box (BorderStyle 3): the \\bord tag then sets the padding."""
    return (
        f"Style: {name},Arial,60,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        f"0,0,0,0,100,100,0,0,3,0,0,2,{int(side_margin)},{int(side_margin)},0,1"
    )


def dialogue(layer: int, start: float, end: float, text: str, style: str = "Default") -> str:
    return f"Dialogue: {layer},{timestamp(start)},{timestamp(end)},{style},,0,0,0,,{text}"
