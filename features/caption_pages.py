"""Caption logic that does not depend on FFmpeg or rendering (so it can be unit tested):

* give every word a start/end time (from speech recognition, or spread across a known time span)
* match the SCRIPT's words (correct spelling) onto the timing found by speech recognition
* group words into caption lines ("pages")
* write / read the human-editable auto-caption.txt  ("MM:SS.s | text")
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.errors import CaptionError
from core.transcribe import Word

# --------------------------------------------------------------------------- words
_PAUSE_WEIGHT = {",": 3.0, ";": 4.0, ":": 4.0, ".": 6.0, "?": 6.0, "!": 6.0, "…": 5.0, "—": 2.0, "–": 2.0}
_TRAILING_QUOTES = "\"'”’)]"


def norm(text: str) -> str:
    """Lower-case letters and digits only, for comparing words ("Lord's," -> "lords")."""
    return re.sub(r"[\W_]+", "", text.lower())


def distribute(tokens: list[str], start: float, end: float) -> list[Word]:
    """Spread words over [start, end] by length. Commas and full stops get a short pause after them."""
    n = len(tokens)
    if n == 0:
        return []
    end = max(end, start + 0.05 * n)
    char_w = [max(2, len(re.sub(r"\W", "", t))) + 0.0 for t in tokens]
    pause_w = []
    for t in tokens:
        core = t.rstrip(_TRAILING_QUOTES)
        pause_w.append(_PAUSE_WEIGHT.get(core[-1:], 0.0) if core else 0.0)
    pause_w[-1] = 0.0
    scale = (end - start) / (sum(char_w) + sum(pause_w))
    words: list[Word] = []
    cursor = 0.0
    for i, token in enumerate(tokens):
        w_start = start + cursor * scale
        w_end = w_start + char_w[i] * scale
        words.append(Word(token, w_start, w_end))
        cursor += char_w[i] + pause_w[i]
    return words


def align_script_to_asr(script: list[str], asr: list[Word], span: tuple[float, float]) -> list[Word]:
    """Put the script's words (with their punctuation and spelling) on the speech-recognition timing.

    Words that both sides agree on take the recognised time directly. Stretches that differ
    ("ten" vs "10", a mis-heard word) are spread over the time the recognised words occupied.
    """
    if not script:
        return []
    a = [norm(w) for w in script]
    b = [norm(w.text) for w in asr]
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    out: list[Word | None] = [None] * len(script)

    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            for k in range(i2 - i1):
                out[i1 + k] = Word(script[i1 + k], asr[j1 + k].start, asr[j1 + k].end)

    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal" or i1 == i2:
            continue
        if j2 > j1:
            t0, t1 = asr[j1].start, asr[j2 - 1].end
        else:
            t0 = out[i1 - 1].end if i1 > 0 and out[i1 - 1] else span[0]
            t1 = out[i2].start if i2 < len(script) and out[i2] else span[1]
        if t1 < t0:
            t0, t1 = t1, t0
        for k, word in enumerate(distribute(script[i1:i2], t0, t1)):
            out[i1 + k] = word

    result: list[Word] = []
    previous_start = span[0]
    for i, word in enumerate(out):
        assert word is not None
        start = max(word.start, previous_start)
        end = max(word.end, start + 0.05)
        result.append(Word(script[i], start, end))
        previous_start = start
    return result


# --------------------------------------------------------------------------- pages
@dataclass
class Page:
    """One caption line: the words shown together. `start`/`end` are the display times."""
    words: list[Word]
    start: float = 0.0
    end: float = 0.0

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


def _chars(words: list[Word]) -> int:
    return sum(len(w.text) + 1 for w in words) - 1


def _segments(group: list[Word], p: dict) -> list[list[Word]]:
    """Split a voiceover file's words at sentence ends and at long silences."""
    segments: list[list[Word]] = []
    current: list[Word] = []
    for word in group:
        if current and word.start - current[-1].end > float(p["max_gap_seconds"]):
            segments.append(current)
            current = []
        current.append(word)
        if p["break_after_sentence"] and word.text.rstrip(_TRAILING_QUOTES)[-1:] in (".", "?", "!", "…"):
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    return segments


def _cut_points(seg: list[Word], k: int, prefer_commas: bool) -> list[int]:
    """Where to cut `seg` into k similar-sized parts (cut i = the new part starts at word i)."""
    n = len(seg)
    cuts: list[int] = []
    for j in range(1, k):
        ideal = round(j * n / k)
        cut = ideal
        if prefer_commas:
            near = [i for i in range(ideal - 2, ideal + 3)
                    if 1 <= i < n and seg[i - 1].text.rstrip(_TRAILING_QUOTES)[-1:] in (",", ";", ":", "—", "–")]
            if near:
                cut = min(near, key=lambda i: abs(i - ideal))
        low = (cuts[-1] + 1) if cuts else 1
        cuts.append(min(max(cut, low), n - (k - j)))
    return cuts


def _split_balanced(seg: list[Word], p: dict) -> list[list[Word]]:
    """One page if it fits, otherwise the fewest similar-sized pages (5+5, never 8+2)."""
    max_words, max_chars = int(p["max_words"]), int(p["max_chars"])
    n = len(seg)
    k = max(-(-n // max_words), -(-_chars(seg) // max_chars), 1)
    while True:
        cuts = _cut_points(seg, k, bool(p["prefer_breaks_at_commas"])) if k > 1 else []
        bounds = [0, *cuts, n]
        parts = [seg[a:b] for a, b in zip(bounds, bounds[1:])]
        if k >= n or all(len(x) <= max_words and _chars(x) <= max_chars for x in parts):
            return parts
        k += 1


def build_pages(groups: list[list[Word]], p: dict) -> list[Page]:
    """Group words into caption lines. A page never spans two voiceover files (groups).

    Lines end at sentence ends and long silences. A sentence too long for one line is cut into
    evenly sized lines, preferably at commas. A very short sentence ("Amen.") joins the line before
    it when everything still fits.
    """
    pages: list[Page] = []
    merge_up_to = int(p["merge_short_sentences_up_to_words"])
    for group in groups:
        group_pages: list[list[Word]] = []
        for seg in _segments(group, p):
            for part in _split_balanced(seg, p):
                prev = group_pages[-1] if group_pages else None
                if (prev is not None and merge_up_to and len(part) <= merge_up_to
                        and part[0].start - prev[-1].end <= float(p["max_gap_seconds"])
                        and len(prev) + len(part) <= int(p["max_words"])
                        and _chars(prev + part) <= int(p["max_chars"])):
                    prev.extend(part)
                else:
                    group_pages.append(list(part))
        pages.extend(Page(words=w) for w in group_pages)
    return pages


def set_display_times(pages: list[Page], t: dict) -> None:
    """Decide when each page appears and disappears (only while the voice speaks)."""
    lead, hold = float(t["lead_in_seconds"]), float(t["hold_after_seconds"])
    min_len, offset = float(t["min_page_seconds"]), float(t["time_offset_seconds"])
    for page in pages:
        page.start = page.words[0].start - lead
        page.end = max(page.words[-1].end + hold, page.start + min_len)
    for i in range(len(pages) - 1):
        nxt = pages[i + 1]
        limit = max(nxt.words[0].start - lead, pages[i].words[-1].end)
        pages[i].end = min(pages[i].end, limit - 0.02)
        nxt.start = max(nxt.start, pages[i].end + 0.01)
        nxt.end = max(nxt.end, nxt.start + 0.2)
    if offset:
        for page in pages:
            page.start += offset
            page.end += offset
            for w in page.words:
                w.start += offset
                w.end += offset
    for page in pages:
        page.start = max(0.0, page.start)
        page.end = max(page.start + 0.1, page.end)


# --------------------------------------------------------------------------- time text
_TIME = r"\d+(?::\d+){1,2}(?:\.\d+)?"
_LINE = re.compile(rf"^\s*({_TIME})(?:\s*-\s*({_TIME}))?\s*\|\s*(.*?)\s*$")


def fmt_time(seconds: float, decimals: int = 1) -> str:
    """12.34 -> '00:12.3' (decimals=1) or '00:12' (decimals=0). Minutes can go above 59."""
    scale = 10 ** decimals
    units = max(0, int(round(seconds * scale)))
    minutes, rest = divmod(units, 60 * scale)
    whole, frac = divmod(rest, scale)
    return f"{minutes:02d}:{whole:02d}" + (f".{frac:0{decimals}d}" if decimals else "")


def parse_time(text: str) -> float:
    """'12:43', '12:43.5' or '1:02:03.5' -> seconds."""
    parts = text.strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError(text)
    seconds = float(parts[-1])
    minutes = int(parts[-2])
    hours = int(parts[0]) if len(parts) == 3 else 0
    return hours * 3600 + minutes * 60 + seconds


# --------------------------------------------------------------------------- edit file
FILE_HEADER = """\
# TheFaithFuel auto-caption file.  One caption (line on screen) per line:   MM:SS.s | text
# Fix spelling and timing here, then set "edit_file.mode" to "use_edited" in settings/caption.json and run again.
#  - the time is when the first word of the line is spoken (minutes:seconds, decimals allowed: 12:43.5)
#  - optional end time:   12:43.5 - 12:47.0 | text
#  - if you only change the text, the words are re-timed across the same line; lines starting with # are ignored
"""


def fingerprint(chunks: list[dict]) -> str:
    rows = [(c.get("label"), round(float(c["start"]), 2), round(float(c["end"]), 2)) for c in chunks]
    return hashlib.sha1(json.dumps(rows).encode("utf-8")).hexdigest()[:16]


def write_edit_files(txt_path: Path, pages: list[Page], fp: str, keep_previous: bool, decimals: int = 1) -> Path:
    """Write the editable text file plus a hidden-detail .json next to it (exact word timing)."""
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    side = txt_path.with_suffix(".json")
    if keep_previous and txt_path.is_file():
        previous = txt_path.with_name(txt_path.stem + ".previous" + txt_path.suffix)
        previous.write_text(txt_path.read_text(encoding="utf-8"), encoding="utf-8")
    lines = [FILE_HEADER.rstrip("\n")]
    for page in pages:
        lines.append(f"{fmt_time(page.words[0].start, decimals)} | {page.text}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    side.write_text(json.dumps({
        "fingerprint": fp,
        "pages": [{"text": pg.text, "start": pg.words[0].start, "end": pg.words[-1].end,
                   "words": [[w.text, w.start, w.end] for w in pg.words]} for pg in pages],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return side


def _same_text(a: str, b: str) -> bool:
    return [norm(x) for x in a.split()] == [norm(x) for x in b.split()]


def _estimate_seconds(tokens: list[str]) -> float:
    weight = sum(max(2, len(re.sub(r"\W", "", t))) for t in tokens)
    return max(0.8, weight * 0.07)


def read_edit_file(txt_path: Path, fp: str, logger) -> list[Page]:
    """Read the edited file back into pages (words re-timed where the text or time was changed)."""
    if not txt_path.is_file():
        raise CaptionError(
            f"edit_file.mode is 'use_edited' but {txt_path} does not exist. Run once with mode 'generate' first."
        )
    side_pages: list[dict] = []
    side = txt_path.with_suffix(".json")
    if side.is_file():
        try:
            data = json.loads(side.read_text(encoding="utf-8"))
            side_pages = data.get("pages", [])
            if data.get("fingerprint") != fp:
                logger.warning(
                    "The voiceover has changed since %s was created, so the times in it may be wrong. "
                    "Set edit_file.mode to 'generate' to rebuild it.", txt_path.name,
                )
        except (ValueError, OSError):
            logger.warning("Could not read %s; re-timing words from the text file only.", side.name)

    entries: list[tuple[float, float | None, str, float]] = []  # start, end, text, rounding step of the start
    for number, raw in enumerate(txt_path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = _LINE.match(raw)
        if not match:
            raise CaptionError(
                f"{txt_path.name}, line {number}: expected 'MM:SS | text' (for example '12:43 | Lord, hear me out.'). Got: {raw[:70]}"
            )
        start = parse_time(match.group(1))
        end = parse_time(match.group(2)) if match.group(2) else None
        frac = match.group(1).partition(".")[2]
        step = 10.0 ** -len(frac) if frac else 1.0
        if match.group(3).strip():
            entries.append((start, end, match.group(3).strip(), step))
    if not entries:
        raise CaptionError(f"{txt_path.name} has no caption lines.")
    entries.sort(key=lambda e: e[0])

    by_index = len(entries) == len(side_pages)
    pool = {}
    if not by_index:
        for sp in side_pages:
            pool.setdefault(" ".join(norm(x) for x in sp["text"].split()), []).append(sp)

    pages: list[Page] = []
    for i, (start, end, text, step) in enumerate(entries):
        tokens = text.split()
        sp = side_pages[i] if by_index else None
        if sp is None:
            key = " ".join(norm(x) for x in tokens)
            if pool.get(key):
                sp = pool[key].pop(0)
        next_start = entries[i + 1][0] if i + 1 < len(entries) else None
        if sp is not None:
            if abs(start - float(sp["start"])) <= step / 2 + 1e-9:
                start = float(sp["start"])  # time untouched: the file only shows it rounded, keep the exact timing
            delta = start - float(sp["start"])
            old_end = float(sp["end"]) + delta
            if _same_text(text, sp["text"]) and end is None:
                words = [Word(tokens[k], float(w[1]) + delta, float(w[2]) + delta)
                         for k, w in enumerate(sp["words"])]
                pages.append(Page(words=words))
                continue
            line_end = end if end is not None else old_end
        else:
            line_end = end if end is not None else start + _estimate_seconds(tokens)
        if next_start is not None and line_end > next_start - 0.05:
            line_end = max(start + 0.3, next_start - 0.05)
        pages.append(Page(words=distribute(tokens, start, max(line_end, start + 0.3))))
    return pages
