"""Turn caption pages into styled ASS events (colour highlight, glow, mixed fonts, animations).

Each page becomes up to two events that animate together:
  layer 0  the glow   (blurred coloured outline around the words that should glow)
  layer 1  the text   (every word with its own font / size / colour; the spoken word is highlighted)
"""
from __future__ import annotations

import random
import re

from core import ass
from features.caption_pages import Page

ROLE_KEYS = ("font", "size", "bold", "italic", "color", "opacity", "text_transform",
             "letter_spacing_px", "outline_px", "outline_color", "shadow_px", "shadow_color", "shadow_opacity")
ANIMATIONS = ("fade", "pop", "bounce", "slide_up", "drop", "blur_in", "reveal")


def resolve_roles(styles: dict) -> dict[str, dict]:
    """base / small / keyword styles, where null in small/keyword means 'same as base'."""
    base = {k: styles["base"][k] for k in ROLE_KEYS}
    roles = {"base": base}
    for name in ("small", "keyword"):
        roles[name] = {k: (styles[name][k] if styles[name][k] is not None else base[k]) for k in ROLE_KEYS}
    return roles


def word_key(text: str) -> str:
    """'Lord's,' -> 'lord' (so 'Lord', 'LORD' and \"Lord's\" all match the keyword 'lord')."""
    t = text.lower()
    t = re.sub(r"['’]s$", "", t)
    return re.sub(r"[^\w]+", "", t)


def classify(text: str, mixed: dict, small: set[str], keywords: set[str]) -> str:
    """'keyword', 'small' or 'base' for a word, following mixed_fonts.apply_to."""
    if not mixed["enabled"]:
        return "base"
    key = word_key(text)
    if "keywords" in mixed["apply_to"] and key in keywords:
        return "keyword"
    if "small_words" in mixed["apply_to"] and key in small:
        return "small"
    return "base"


def transform(text: str, mode: str) -> str:
    if mode == "upper":
        return text.upper()
    if mode == "lower":
        return text.lower()
    if mode == "title":
        return text.title()
    return text


def _ms(seconds: float) -> int:
    return max(0, int(round(seconds * 1000)))


def animation_tags(name: str, x: int, y: int, in_ms: int, out_ms: int) -> tuple[str, str]:
    """(tags for both layers, extra tags for the text layer only) for one animation."""
    pos = f"\\an2\\pos({x},{y})"
    fad = f"\\fad({in_ms},{out_ms})"
    if name == "pop":
        return f"{pos}\\fscx80\\fscy80\\t(0,{in_ms},\\fscx100\\fscy100)\\fad({in_ms // 2},{out_ms})", ""
    if name == "bounce":
        a = int(in_ms * 0.6)
        return (f"{pos}\\fscx60\\fscy60\\t(0,{a},\\fscx112\\fscy112)\\t({a},{in_ms},\\fscx100\\fscy100)"
                f"\\fad({in_ms // 3},{out_ms})"), ""
    if name == "slide_up":
        return f"\\an2\\move({x},{y + 60},{x},{y},0,{in_ms}){fad}", ""
    if name == "drop":
        return f"\\an2\\move({x},{y - 60},{x},{y},0,{in_ms}){fad}", ""
    if name == "blur_in":
        return f"{pos}{fad}", f"\\blur14\\t(0,{in_ms + 60},\\blur0)"
    return f"{pos}{fad}", ""  # fade and reveal


def choose_animations(count: int, a: dict, rng: random.Random) -> list[str]:
    """One animation per page, following animation.mode. Never the same one twice in a row."""
    mode, pool = a["mode"], list(a["pool"])
    if mode == "fixed":
        return [a["fixed"]] * count
    if mode == "random_once":
        return [rng.choice(pool)] * count
    chosen: list[str] = []
    for _ in range(count):
        options = [p for p in pool if p != (chosen[-1] if chosen else None)] or pool
        chosen.append(rng.choice(options))
    return chosen


def _run_style_tags(role: dict) -> str:
    return (
        f"\\fn{role['font']}\\fs{role['size']}\\b{1 if role['bold'] else 0}\\i{1 if role['italic'] else 0}"
        f"\\fsp{role['letter_spacing_px']}"
    )


def build_events(pages: list[Page], s: dict, width: int, height: int, rng: random.Random,
                 time_shift: float = 0.0, limit: float | None = None) -> tuple[list[str], dict]:
    """Return (ASS 'Dialogue:' lines, how many times each animation was used).

    `time_shift` is subtracted from every time and `limit` drops later pages (used by the preview).
    """
    roles = resolve_roles(s["styles"])
    mixed = s["mixed_fonts"]
    small = {word_key(w) for w in mixed["small_words"]}
    keywords = {word_key(w) for w in mixed["keywords"]}
    hl, gl, an, pos = s["highlight"], s["glow"], s["animation"], s["position"]

    x = width // 2
    y = int(height - int(pos["bottom_margin_px"]) + int(pos["y_offset_px"]))
    in_ms, out_ms = int(an["in_ms"]), int(an["out_ms"])
    tr = int(hl["transition_ms"])
    bridge = float(hl["bridge_gaps_under_seconds"])

    hl_color = ass.color(hl["color"], "highlight.color")
    glow_color = ass.color(gl["color"], "glow.color")
    glow_alpha = ass.alpha(gl["opacity"])
    glow_on_active = gl["enabled"] and "active_word" in gl["apply_to"]
    glow_on_keywords = gl["enabled"] and "keywords" in gl["apply_to"]
    glow_on_all = gl["enabled"] and "all" in gl["apply_to"]

    names = choose_animations(len(pages), an, rng)
    used: dict[str, int] = {}
    events: list[str] = []

    for page, anim in zip(pages, names):
        p_start, p_end = page.start - time_shift, page.end - time_shift
        if p_end <= 0 or (limit is not None and p_start >= limit):
            continue
        if time_shift and p_start < 0:  # preview starts in the middle of this page: skip it
            continue
        used[anim] = used.get(anim, 0) + 1
        both, text_only = animation_tags(anim, x, y, in_ms, out_ms)
        reveal = anim == "reveal"

        text_runs: list[str] = []
        glow_runs: list[str] = []
        n = len(page.words)
        for i, word in enumerate(page.words):
            role_name = classify(word.text, mixed, small, keywords)
            role = roles[role_name]
            shown = ass.escape_text(transform(word.text, role["text_transform"])) + (" " if i < n - 1 else "")

            # when this word is "active" (relative to the page start, in ms)
            w_start = word.start - time_shift
            w_end = page.words[i + 1].start - time_shift if i + 1 < n else word.end - time_shift
            if i + 1 < n and page.words[i + 1].start - word.end > bridge:
                w_end = word.end - time_shift
            a = _ms(w_start - p_start)
            b = max(a + 40, _ms(w_end - p_start))

            base_color = ass.color(role["color"], f"styles.{role_name}.color")
            outline_color = ass.color(role["outline_color"], f"styles.{role_name}.outline_color")
            shadow_color = ass.color(role["shadow_color"], f"styles.{role_name}.shadow_color")
            fill_alpha = ass.alpha(role["opacity"])
            shadow_alpha = ass.alpha(role["shadow_opacity"])

            tags = (
                f"{_run_style_tags(role)}\\1c{base_color}\\1a{fill_alpha}\\3c{outline_color}"
                f"\\bord{role['outline_px']}\\shad{role['shadow_px']}\\4c{shadow_color}\\4a{shadow_alpha}"
            )
            if reveal:
                tags += f"\\alpha&HFF&\\t({a},{a + 80},\\1a{fill_alpha}\\3a&H00&\\4a{shadow_alpha})"
            if hl["enabled"]:
                tags += f"\\t({a},{a + tr},\\1c{hl_color})"
                if hl["after_spoken"] == "base":
                    tags += f"\\t({b},{b + tr},\\1c{base_color})"
                elif hl["after_spoken"] == "dim":
                    tags += f"\\t({b},{b + tr},\\1c{base_color}\\1a{ass.alpha(hl['dim_opacity'])})"
            text_runs.append("{" + tags + "}" + shown)

            # glow layer: same shapes, invisible fill, blurred coloured outline
            always = glow_on_all or (glow_on_keywords and role_name == "keyword")
            g = (f"{_run_style_tags(role)}\\1a&HFF&\\3c{glow_color}\\bord{gl['size_px']}\\shad0"
                 f"\\blur{gl['blur']}\\3a{glow_alpha if always else '&HFF&'}")
            if glow_on_active and not always:
                g += f"\\t({a},{a + tr},\\3a{glow_alpha})\\t({b},{b + tr * 2},\\3a&HFF&)"
            glow_runs.append("{" + g + "}" + shown)

        if gl["enabled"]:
            events.append(ass.dialogue(0, p_start, p_end, "{" + both + "}" + "".join(glow_runs)))
        events.append(ass.dialogue(1, p_start, p_end, "{" + both + text_only + "}" + "".join(text_runs)))
    return events, used
