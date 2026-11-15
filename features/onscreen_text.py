"""Feature 5 - On-screen text.

You list the texts in settings/onscreen_text.json, one per line, as  "start | stop | text":

    "items": ["0:05 | 0:14 | Praying for Peace", "4:14 | 5:45 | Amen"]

Times are  seconds (75),  M:SS (1:15),  H:MM:SS (1:02:03)  and may have decimals (1:15.5).
A text can have several lines: write \\n inside it ("Line one\\nLine two").

Stage (see pipeline.py):
  prepare(ctx)  reads and checks the items, writes onscreen_text.ass and registers it, so the clip
                render burns it in (no extra video encode). It runs right after the captions.
  preview(...)  renders a short test video (your first clip + the texts) for tuning the look fast.

One animation is used for ALL texts of a video (picked at random for each video by default).
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path

from core import ass
from core import ffmpeg_tools as ff
from core.config_loader import choice, flag, load_feature_settings, num
from core.context import Context
from core.errors import ConfigError, TextError
from core.files import list_files
from core.fonts import missing_fonts
from features import captions
from features.caption_style import ANIMATIONS, transform

SETTINGS_FILE = "onscreen_text.json"
VIDEO_EXTENSIONS = captions.VIDEO_EXTENSIONS

# anchor -> (ASS alignment number, horizontal side, vertical side)
ANCHORS = {
    "top_left": (7, "left", "top"), "top_center": (8, "center", "top"), "top_right": (9, "right", "top"),
    "middle_left": (4, "left", "middle"), "center": (5, "center", "middle"), "middle_right": (6, "right", "middle"),
    "bottom_left": (1, "left", "bottom"), "bottom_center": (2, "center", "bottom"),
    "bottom_right": (3, "right", "bottom"),
}
MODES = ("random_once", "fixed", "random_per_item")

DEFAULTS: dict = {
    "check_ffmpeg_filter": True,
    "items": [],
    "position": {
        "anchor": "top_center", "margin_x_px": 120, "margin_y_px": 90,
        "x_offset_px": 0, "y_offset_px": 0, "side_margin_px": 160,
    },
    "style": {
        "font": "Segoe UI", "size": 72, "bold": True, "italic": False, "color": "#FFFFFF", "opacity": 1.0,
        "text_transform": "none", "letter_spacing_px": 0, "outline_px": 3, "outline_color": "#000000",
        "shadow_px": 2, "shadow_color": "#000000", "shadow_opacity": 0.5,
    },
    "box": {"enabled": False, "color": "#000000", "opacity": 0.55, "padding_px": 22},
    "glow": {"enabled": True, "color": "#4DE3FF", "size_px": 6, "blur": 12, "opacity": 0.85},
    "animation": {
        "mode": "random_once", "fixed": "fade",
        "pool": ["fade", "pop", "slide_up", "drop", "blur_in", "reveal"],
        "in_ms": 450, "out_ms": 450, "reveal_word_ms": 160,
    },
}


# --------------------------------------------------------------------------- items
@dataclass
class Item:
    number: int       # position in the list (1-based), for messages
    start: float
    stop: float
    text: str


_CLOCK = re.compile(r"^\d+(?::\d+){0,2}(?:\.\d+)?$")


def parse_time(value: str, where: str) -> float:
    """'75' / '1:15' / '1:02:03' / '1:15.5' -> seconds."""
    text = value.strip()
    if not _CLOCK.match(text):
        raise ConfigError(f"{where}: '{value.strip()}' is not a time. Use seconds (75), M:SS (1:15) or H:MM:SS (1:02:03).")
    parts = text.split(":")
    seconds = float(parts[-1])
    minutes = int(parts[-2]) if len(parts) >= 2 else 0
    hours = int(parts[-3]) if len(parts) == 3 else 0
    if len(parts) >= 2 and seconds >= 60:
        raise ConfigError(f"{where}: seconds must be below 60 in '{text}' (write 1:15 instead of 0:75).")
    if len(parts) == 3 and minutes >= 60:
        raise ConfigError(f"{where}: minutes must be below 60 in '{text}'.")
    return hours * 3600 + minutes * 60 + seconds


def parse_items(rows) -> list[Item]:
    """Turn the "start | stop | text" strings into Items (sorted by start time)."""
    if not isinstance(rows, list):
        raise ConfigError("'items' must be a list of lines like \"4:14 | 5:45 | Title\".")
    items: list[Item] = []
    for number, row in enumerate(rows, 1):
        where = f"items[{number}]"
        if not isinstance(row, str):
            raise ConfigError(f"{where} must be a text line like \"4:14 | 5:45 | Title\" (got {row!r}).")
        line = row.strip()
        if not line or line.startswith("#"):
            continue  # blank lines and #comments are ignored
        parts = line.split("|", 2)
        if len(parts) != 3:
            raise ConfigError(f"{where}: '{line}' must look like \"start | stop | text\", for example \"4:14 | 5:45 | Title\".")
        start, stop = parse_time(parts[0], where), parse_time(parts[1], where)
        text = parts[2].strip().replace("\\n", "\n")
        if not text.replace("\n", "").strip():
            raise ConfigError(f"{where}: the text after the second '|' is empty.")
        if stop <= start:
            raise ConfigError(f"{where}: stop ({parts[1].strip()}) must be later than start ({parts[0].strip()}).")
        items.append(Item(number, start, stop, text))
    return sorted(items, key=lambda i: (i.start, i.number))


def fit_to_length(items: list[Item], length: float | None, log) -> list[Item]:
    """Drop items that start after the video ends; cut ones that run past the end."""
    if length is None:
        return items
    kept: list[Item] = []
    for item in items:
        if item.start >= length:
            log.warning("On-screen text item %d ('%s') starts at %.1fs, after the video ends (%.1fs) - skipped.",
                        item.number, item.text.splitlines()[0], item.start, length)
            continue
        if item.stop > length:
            log.info("On-screen text item %d runs past the end of the video; it is cut at %.1fs.", item.number, length)
            item = Item(item.number, item.start, length, item.text)
        kept.append(item)
    return kept


def overlapping(items: list[Item]) -> list[tuple[Item, Item]]:
    pairs = []
    for i, first in enumerate(items):
        for second in items[i + 1:]:
            if second.start >= first.stop:
                break
            pairs.append((first, second))
    return pairs


# --------------------------------------------------------------------------- settings
def validate_settings(s: dict) -> None:
    flag(s["check_ffmpeg_filter"], "check_ffmpeg_filter")
    parse_items(s["items"])

    p = s["position"]
    choice(p["anchor"], tuple(ANCHORS), "position.anchor")
    for key in ("margin_x_px", "margin_y_px"):
        num(p[key], f"position.{key}", 0, 2000)
    for key in ("x_offset_px", "y_offset_px"):
        num(p[key], f"position.{key}", -3000, 3000)
    num(p["side_margin_px"], "position.side_margin_px", 0, 900)

    st = s["style"]
    if not isinstance(st["font"], str) or not st["font"].strip():
        raise ConfigError("'style.font' must be a font name such as \"Segoe UI\".")
    num(st["size"], "style.size", 8, 600)
    flag(st["bold"], "style.bold")
    flag(st["italic"], "style.italic")
    for key in ("color", "outline_color", "shadow_color"):
        ass.parse_hex_color(st[key], f"style.{key}")
    for key in ("opacity", "shadow_opacity"):
        num(st[key], f"style.{key}", 0, 1)
    choice(st["text_transform"], ("none", "upper", "lower", "title"), "style.text_transform")
    num(st["letter_spacing_px"], "style.letter_spacing_px", -20, 60)
    for key in ("outline_px", "shadow_px"):
        num(st[key], f"style.{key}", 0, 40)

    b = s["box"]
    flag(b["enabled"], "box.enabled")
    ass.parse_hex_color(b["color"], "box.color")
    num(b["opacity"], "box.opacity", 0, 1)
    num(b["padding_px"], "box.padding_px", 0, 200)

    g = s["glow"]
    flag(g["enabled"], "glow.enabled")
    ass.parse_hex_color(g["color"], "glow.color")
    num(g["size_px"], "glow.size_px", 0, 60)
    num(g["blur"], "glow.blur", 0, 60)
    num(g["opacity"], "glow.opacity", 0, 1)

    a = s["animation"]
    choice(a["mode"], MODES, "animation.mode")
    choice(a["fixed"], ANIMATIONS, "animation.fixed")
    if not isinstance(a["pool"], list) or not a["pool"] or any(x not in ANIMATIONS for x in a["pool"]):
        raise ConfigError(f"'animation.pool' must be a non-empty list using only {list(ANIMATIONS)}.")
    num(a["in_ms"], "animation.in_ms", 0, 5000)
    num(a["out_ms"], "animation.out_ms", 0, 5000)
    num(a["reveal_word_ms"], "animation.reveal_word_ms", 20, 3000)


def load_settings(ctx: Context) -> dict:
    s = load_feature_settings(ctx.paths["settings"], SETTINGS_FILE, DEFAULTS, ctx.config_warnings)
    validate_settings(s)
    for warning in ctx.config_warnings:
        ctx.logger.warning(warning)
    ctx.config_warnings.clear()
    return s


# --------------------------------------------------------------------------- ass file
def choose_animations(count: int, a: dict, rng: random.Random) -> list[str]:
    if a["mode"] == "fixed":
        return [a["fixed"]] * count
    if a["mode"] == "random_per_item":
        return [rng.choice(a["pool"]) for _ in range(count)]
    return [rng.choice(a["pool"])] * count  # random_once: one random animation for the whole video


def anchor_point(s: dict, width: int, height: int) -> tuple[int, int, int]:
    """(ASS alignment number, x, y) in pixels from position.*"""
    p = s["position"]
    an, h, v = ANCHORS[p["anchor"]]
    x = {"left": int(p["margin_x_px"]), "center": width // 2, "right": width - int(p["margin_x_px"])}[h]
    y = {"top": int(p["margin_y_px"]), "middle": height // 2, "bottom": height - int(p["margin_y_px"])}[v]
    return an, x + int(p["x_offset_px"]), y + int(p["y_offset_px"])


def _ms(seconds: float) -> int:
    return max(0, int(round(seconds * 1000)))


def motion_tags(name: str, an: int, x: int, y: int, in_ms: int, out_ms: int) -> tuple[str, str]:
    """(tags for every layer, extra tags for the text layer only)."""
    pos = f"\\an{an}\\pos({x},{y})"
    fad = f"\\fad({in_ms},{out_ms})"
    if name == "pop":
        return f"{pos}\\fscx80\\fscy80\\t(0,{in_ms},\\fscx100\\fscy100){fad}", ""
    if name == "bounce":
        a = int(in_ms * 0.6)
        return f"{pos}\\fscx60\\fscy60\\t(0,{a},\\fscx112\\fscy112)\\t({a},{in_ms},\\fscx100\\fscy100){fad}", ""
    if name == "slide_up":
        return f"\\an{an}\\move({x},{y + 60},{x},{y},0,{in_ms}){fad}", ""
    if name == "drop":
        return f"\\an{an}\\move({x},{y - 60},{x},{y},0,{in_ms}){fad}", ""
    if name == "blur_in":
        return f"{pos}{fad}", f"\\blur14\\t(0,{in_ms + 60},\\blur0)"
    return f"{pos}{fad}", ""  # fade and reveal


def _font_tags(st: dict) -> str:
    return (f"\\fn{st['font']}\\fs{int(st['size'])}\\b{1 if st['bold'] else 0}\\i{1 if st['italic'] else 0}"
            f"\\fsp{st['letter_spacing_px']:g}")


def _runs(lines: list[str], base: str, per_word: str | None, step_ms: int) -> str:
    """The text as ASS runs. With `per_word` (a tag template containing {a} and {b}) every word
    fades in `step_ms` after the previous one; otherwise it is one run."""
    if per_word is None:
        return "{" + base + "}" + "\\N".join(lines)
    out: list[str] = []
    index = 0
    for li, line in enumerate(lines):
        words = line.split()
        for wi, word in enumerate(words):
            a = index * step_ms
            tail = "\\N" if (wi == len(words) - 1 and li < len(lines) - 1) else (" " if wi < len(words) - 1 else "")
            out.append("{" + base + per_word.format(a=a, b=a + 120) + "}" + word + tail)
            index += 1
    return "".join(out)


def build_events(items: list[Item], s: dict, width: int, height: int, rng: random.Random,
                 time_shift: float = 0.0, limit: float | None = None) -> tuple[list[str], dict]:
    """(ASS 'Dialogue:' lines, how many items used each animation)."""
    st, box, gl, an_cfg = s["style"], s["box"], s["glow"], s["animation"]
    an, x, y = anchor_point(s, width, height)
    names = choose_animations(len(items), an_cfg, rng)
    fill_alpha, shadow_alpha = ass.alpha(st["opacity"]), ass.alpha(st["shadow_opacity"])
    text_color = ass.color(st["color"], "style.color")
    outline_color = ass.color(st["outline_color"], "style.outline_color")
    shadow_color = ass.color(st["shadow_color"], "style.shadow_color")
    glow_alpha = ass.alpha(gl["opacity"])

    events: list[str] = []
    used: dict[str, int] = {}
    for item, anim in zip(items, names):
        start, stop = item.start - time_shift, item.stop - time_shift
        if stop <= 0 or (limit is not None and start >= limit):
            continue
        start = max(0.0, start)
        duration = stop - start
        in_ms, out_ms = int(an_cfg["in_ms"]), int(an_cfg["out_ms"])
        if in_ms + out_ms > duration * 1000:  # a very short text: shrink the fades so they still fit
            in_ms = out_ms = int(round(duration * 500))
        used[anim] = used.get(anim, 0) + 1

        lines = [ass.escape_text(transform(line.strip(), st["text_transform"])) for line in item.text.split("\n")]
        lines = [line for line in lines if line]
        both, text_only = motion_tags(anim, an, x, y, in_ms, out_ms)
        reveal = anim == "reveal"
        step = int(min(int(an_cfg["reveal_word_ms"]), max(20, duration * 600 / max(1, sum(len(l.split()) for l in lines)))))
        font = _font_tags(st)

        if box["enabled"]:
            box_tags = (f"{font}\\1a&HFF&\\3c{ass.color(box['color'], 'box.color')}\\3a{ass.alpha(box['opacity'])}"
                        f"\\bord{int(box['padding_px'])}\\shad0")
            events.append(ass.dialogue(0, start, stop, "{" + both + "}" + _runs(lines, box_tags, None, step), "Box"))
        if gl["enabled"]:
            glow_tags = (f"{font}\\1a&HFF&\\3c{ass.color(gl['color'], 'glow.color')}\\bord{int(gl['size_px'])}"
                         f"\\shad0\\blur{gl['blur']}\\3a{'&HFF&' if reveal else glow_alpha}")
            events.append(ass.dialogue(1, start, stop, "{" + both + "}" + _runs(
                lines, glow_tags, "\\t({a},{b},\\3a" + glow_alpha + ")" if reveal else None, step)))
        text_tags = (f"{font}\\1c{text_color}\\1a{fill_alpha}\\3c{outline_color}\\bord{int(st['outline_px'])}"
                     f"\\shad{int(st['shadow_px'])}\\4c{shadow_color}\\4a{shadow_alpha}")
        if reveal:
            text_tags += "\\1a&HFF&\\3a&HFF&\\4a&HFF&"
        events.append(ass.dialogue(2, start, stop, "{" + both + text_only + "}" + _runs(
            lines, text_tags,
            "\\t({a},{b},\\1a" + fill_alpha + "\\3a&H00&\\4a" + shadow_alpha + ")" if reveal else None, step)))
    return events, used


def write_ass(ctx: Context, s: dict, items: list[Item], *, name: str = "onscreen_text.ass",
              time_shift: float = 0.0, limit: float | None = None) -> Path:
    width, height = int(ctx.cfg["video"]["width"]), int(ctx.cfg["video"]["height"])
    rng = random.Random(ctx.seed ^ 0x7E47)  # its own generator: never changes the clip/caption choices
    events, used = build_events(items, s, width, height, rng, time_shift=time_shift, limit=limit)
    if not events:
        raise TextError("None of the on-screen text items falls inside the selected time range.")
    side = int(s["position"]["side_margin_px"])
    st = s["style"]
    text = ass.header(width, height, side_margin=side, font=str(st["font"]), size=int(st["size"]),
                      title="TheFaithFuel on-screen text", extra_styles=(ass.box_style("Box", side),))
    text += "\n".join(events) + "\n"
    ctx.work_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.work_dir / name
    path.write_text(text, encoding="utf-8")
    ctx.logger.info("On-screen text: %d item(s), animation: %s", sum(used.values()),
                    ", ".join(f"{k} x{v}" for k, v in sorted(used.items())))
    gone = missing_fonts([str(st["font"])])
    if gone:
        ctx.logger.warning("Font not found on this PC: %s. Windows will substitute another font. Run  "
                           "py -3.12 main.py --list-fonts  to see the exact names for settings/onscreen_text.json.",
                           ", ".join(gone))
    return path


def _require_filter(ctx: Context, s: dict) -> None:
    captions.require_ass_filter(ctx, s, what="on-screen text", feature="onscreen_text",
                                settings_file=SETTINGS_FILE)


def _warn_overlaps(ctx: Context, items: list[Item]) -> None:
    for a, b in overlapping(items):
        ctx.logger.warning("On-screen text items %d and %d overlap in time, so they are drawn on top of each other. "
                           "Change their times in settings/onscreen_text.json.", a.number, b.number)


# --------------------------------------------------------------------------- stage: prepare
def prepare(ctx: Context) -> None:
    s = load_settings(ctx)
    items = parse_items(s["items"])
    if not items:
        ctx.logger.info("On-screen text: no items in settings/onscreen_text.json - nothing to show.")
        return
    items = fit_to_length(items, ctx.target_duration, ctx.logger)
    if not items:
        ctx.logger.warning("On-screen text: every item is outside the video length - nothing to show.")
        return
    _warn_overlaps(ctx, items)
    if ctx.dry_run:
        ctx.logger.info("On-screen text: %d item(s) checked (skipped in a dry run; use --text-preview to test them).",
                        len(items))
        return
    _require_filter(ctx, s)
    path = write_ass(ctx, s, items)
    ctx.state.setdefault("video_post_filters", []).append(f"ass={path.name}")


# --------------------------------------------------------------------------- preview
def preview(ctx: Context, start: float | None, length: float) -> Path:
    """A short test video (your first clip, looped, no sound) with the texts on it."""
    s = load_settings(ctx)
    items = parse_items(s["items"])
    if not items:
        raise ConfigError("--text-preview needs at least one line in 'items' in settings/onscreen_text.json, "
                          "for example \"0:02 | 0:10 | My title\".")
    _require_filter(ctx, s)
    _warn_overlaps(ctx, items)
    if start is None:
        start = max(0.0, items[0].start - 1.0)
    path = write_ass(ctx, s, items, name="onscreen_preview.ass", time_shift=start, limit=length)

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
    out = ctx.paths["output"] / "text_preview.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    args = [*source, "-t", f"{length:.3f}", "-vf", f"{chain},ass={path.name}", "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(out)]
    ff.run_ffmpeg(ctx.ffmpeg, args, total_seconds=length, log_path=ctx.paths["logs"] / "ffmpeg_text_preview.log",
                  label="text preview", logger=ctx.logger, loglevel=str(ctx.cfg["ffmpeg"]["log_level"]),
                  cwd=ctx.work_dir)
    return out
