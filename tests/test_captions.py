"""Unit tests for the caption logic (no FFmpeg needed).  Run:  py -3.12 -m unittest discover -s tests -v"""
import logging
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import ass  # noqa: E402
from core.config_loader import deep_merge  # noqa: E402
from core.errors import CaptionError, ConfigError  # noqa: E402
from core.transcribe import Word  # noqa: E402
from features import caption_pages as cp  # noqa: E402
from features import caption_style as cs  # noqa: E402
from features import captions  # noqa: E402

LOG = logging.getLogger("test")


def words(*items):
    return [Word(t, s, e) for t, s, e in items]


def settings(**over):
    s = deep_merge(captions.DEFAULTS, {}, [])
    for k, v in over.items():
        s[k] = v
    return s


class TimeText(unittest.TestCase):
    def test_format_and_parse(self):
        self.assertEqual(cp.fmt_time(763.0), "12:43.0")
        self.assertEqual(cp.fmt_time(5.14), "00:05.1")
        self.assertEqual(cp.fmt_time(59.96), "01:00.0")  # rounds up into the next minute, never '00:60.0'
        self.assertEqual(cp.parse_time("12:43"), 763.0)
        self.assertAlmostEqual(cp.parse_time("12:43.5"), 763.5)
        self.assertEqual(cp.parse_time("1:02:03"), 3723.0)
        for t in (0.0, 4.4, 61.7, 763.5, 4000.0):
            self.assertAlmostEqual(cp.parse_time(cp.fmt_time(t)), t, places=1)


class Distribute(unittest.TestCase):
    def test_fills_span_in_order(self):
        w = cp.distribute("Lord, hear me out.".split(), 10.0, 14.0)
        self.assertEqual(len(w), 4)
        self.assertAlmostEqual(w[0].start, 10.0)
        self.assertAlmostEqual(w[-1].end, 14.0)
        for a, b in zip(w, w[1:]):
            self.assertLess(a.start, a.end)
            self.assertLessEqual(a.end, b.start + 1e-9)

    def test_comma_leaves_a_gap(self):
        w = cp.distribute(["Lord,", "hear"], 0, 3)
        self.assertGreater(w[1].start - w[0].end, 0.2)

    def test_empty(self):
        self.assertEqual(cp.distribute([], 0, 1), [])


class Align(unittest.TestCase):
    def test_script_spelling_with_recognised_timing(self):
        script = ["Heavenly", "Father,", "we", "come", "to", "You."]
        heard = words(("heavenly", 1.0, 1.5), ("father", 1.5, 2.0), ("we", 2.2, 2.4),
                      ("come", 2.4, 2.8), ("to", 2.8, 2.9), ("you", 2.9, 3.4))
        out = cp.align_script_to_asr(script, heard, (0, 5))
        self.assertEqual([w.text for w in out], script)  # script spelling and punctuation kept
        self.assertEqual((out[0].start, out[5].end), (1.0, 3.4))

    def test_mismatch_is_spread_over_heard_time(self):
        script = ["I", "pray", "ten", "times", "daily."]
        heard = words(("I", 0.0, 0.2), ("pray", 0.2, 0.6), ("10", 0.7, 1.0), ("times", 1.0, 1.4), ("daily", 1.4, 2.0))
        out = cp.align_script_to_asr(script, heard, (0, 3))
        self.assertAlmostEqual(out[2].start, 0.7)
        self.assertAlmostEqual(out[2].end, 1.0)

    def test_missing_and_extra_recognised_words_stay_monotonic(self):
        script = ["one", "two", "three", "four", "five"]
        heard = words(("one", 0.0, 0.3), ("uh", 0.3, 0.4), ("four", 2.0, 2.4), ("five", 2.4, 2.8))
        out = cp.align_script_to_asr(script, heard, (0, 4))
        self.assertEqual(len(out), 5)
        starts = [w.start for w in out]
        self.assertEqual(starts, sorted(starts))
        self.assertTrue(all(w.end > w.start for w in out))
        self.assertAlmostEqual(out[3].start, 2.0)

    def test_nothing_heard_in_between_uses_gap(self):
        script = ["a1", "b2", "c3"]
        heard = words(("a1", 0.0, 0.5), ("c3", 3.0, 3.5))
        out = cp.align_script_to_asr(script, heard, (0, 4))
        self.assertTrue(0.4 <= out[1].start <= 3.0)


class Pages(unittest.TestCase):
    P = captions.DEFAULTS["page"]

    def test_breaks_after_sentence_and_respects_max_words(self):
        g = [Word(t, i * 0.4, i * 0.4 + 0.3) for i, t in enumerate(
            "Father we thank You today. We praise Your name and we worship You with all our hearts.".split())]
        pages = cp.build_pages([g], self.P)
        self.assertEqual(pages[0].text, "Father we thank You today.")
        self.assertTrue(all(len(p.words) <= 8 for p in pages))
        self.assertEqual(sum(len(p.words) for p in pages), len(g))

    def test_big_gap_and_group_boundary_start_new_page(self):
        a = words(("one", 0, 0.3), ("two", 0.4, 0.7), ("three", 3.0, 3.3))
        pages = cp.build_pages([a, words(("four", 10, 10.3))], self.P)
        self.assertEqual([p.text for p in pages], ["one two", "three", "four"])

    def test_long_sentence_is_split_evenly_not_8_plus_2(self):
        g = [Word(x, i * 0.4, i * 0.4 + 0.3) for i, x in enumerate("Heavenly Father we come to You today with grateful hearts.".split())]
        pages = cp.build_pages([g], self.P)
        self.assertEqual([len(p.words) for p in pages], [5, 5])

    def test_prefers_to_cut_at_a_comma(self):
        g = [Word(x, i * 0.4, i * 0.4 + 0.3) for i, x in enumerate(
            "Thank You Lord, for all the grace and mercy You pour out on us daily.".split())]
        pages = cp.build_pages([g], self.P)
        self.assertTrue(pages[0].text.endswith("Lord,") or len(pages) == 2)
        self.assertTrue(all(len(p.words) <= 8 for p in pages))

    def test_fitting_sentence_is_not_split_at_commas(self):
        g = [Word(x, i * 0.3, i * 0.3 + 0.2) for i, x in enumerate("Lord, we come to You, Father, today".split())]
        self.assertEqual(len(cp.build_pages([g], self.P)), 1)

    def test_short_last_sentence_joins_the_line_before(self):
        g = [Word(x, i * 0.4, i * 0.4 + 0.3) for i, x in enumerate("Jesus, we trust in Your faithful love. Amen.".split())]
        pages = cp.build_pages([g], self.P)
        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0].text.endswith("love. Amen."))

    def test_short_sentence_not_merged_when_it_would_not_fit_or_after_a_gap(self):
        g = [Word(x, i * 0.4, i * 0.4 + 0.3) for i, x in enumerate("One two three four five six seven eight. Amen.".split())]
        self.assertEqual(len(cp.build_pages([g], self.P)), 2)  # 8 + 1 > max_words
        g2 = words(("Praise", 0, 0.4), ("Him.", 0.4, 0.8), ("Amen.", 4.0, 4.4))
        self.assertEqual(len(cp.build_pages([g2], self.P)), 2)  # 3.2 s of silence in between

    def test_display_times_only_while_speaking_and_no_overlap(self):
        pages = cp.build_pages([words(("hello", 5.0, 5.4), ("there.", 5.4, 5.9)),
                                words(("again", 6.0, 6.4))], self.P)
        cp.set_display_times(pages, captions.DEFAULTS["timing"])
        self.assertAlmostEqual(pages[0].start, 5.0 - 0.08)
        self.assertLessEqual(pages[0].end, pages[1].start)
        self.assertLessEqual(pages[0].end, 6.0)  # never lingers into the next page / long silence
        self.assertGreaterEqual(pages[0].start, 0)

    def test_time_offset_moves_everything(self):
        pages = cp.build_pages([words(("hi", 5.0, 5.4))], self.P)
        t = dict(captions.DEFAULTS["timing"], time_offset_seconds=0.5)
        cp.set_display_times(pages, t)
        self.assertAlmostEqual(pages[0].words[0].start, 5.5)


class EditFile(unittest.TestCase):
    def make_pages(self):
        pages = cp.build_pages([words(
            ("Lord,", 5.0, 5.4), ("hear", 5.5, 5.8), ("me", 5.8, 6.0), ("out.", 6.0, 6.5),
            ("Thank", 9.0, 9.4), ("You", 9.4, 9.6), ("Jesus.", 9.6, 10.2))], captions.DEFAULTS["page"])
        return pages

    def roundtrip(self, mutate=None):
        pages = self.make_pages()
        with tempfile.TemporaryDirectory() as d:
            txt = Path(d) / "auto-caption.txt"
            cp.write_edit_files(txt, pages, "fp1", keep_previous=True)
            if mutate:
                mutate(txt)
            return pages, cp.read_edit_file(txt, "fp1", LOG), txt.read_text(encoding="utf-8")

    def test_file_format_matches_what_you_asked_for(self):
        _, _, text = self.roundtrip()
        self.assertIn("00:05.0 | Lord, hear me out.", text)
        self.assertIn("00:09.0 | Thank You Jesus.", text)

    def test_unchanged_file_gives_identical_timing(self):
        pages, back, _ = self.roundtrip()
        self.assertEqual(len(back), len(pages))
        for a, b in zip(pages, back):
            self.assertEqual(a.text, b.text)
            for x, y in zip(a.words, b.words):
                self.assertAlmostEqual(x.start, y.start, places=6)
                self.assertAlmostEqual(x.end, y.end, places=6)

    def test_changed_time_shifts_whole_line(self):
        _, back, _ = self.roundtrip(lambda p: p.write_text(
            p.read_text(encoding="utf-8").replace("00:05.0 |", "00:06.0 |"), encoding="utf-8"))
        self.assertAlmostEqual(back[0].words[0].start, 6.0)
        self.assertAlmostEqual(back[0].words[-1].end, 7.5)  # same length as before (1.5 s)

    def test_changed_text_is_retimed_inside_the_same_line(self):
        _, back, _ = self.roundtrip(lambda p: p.write_text(
            p.read_text(encoding="utf-8").replace("Lord, hear me out.", "Lord, please hear my cry out to You."), encoding="utf-8"))
        self.assertEqual(back[0].text, "Lord, please hear my cry out to You.")
        self.assertAlmostEqual(back[0].words[0].start, 5.0)
        self.assertAlmostEqual(back[0].words[-1].end, 6.5, places=2)
        self.assertEqual(sorted(w.start for w in back[0].words), [w.start for w in back[0].words])

    def test_whole_minutes_format_from_the_request(self):
        _, back, _ = self.roundtrip(lambda p: p.write_text("12:43 | Lord, hear me out.\n", encoding="utf-8"))
        self.assertEqual(back[0].words[0].start, 763.0)

    def test_added_line_and_explicit_end(self):
        def edit(p):
            p.write_text(p.read_text(encoding="utf-8") + "00:12.0 - 00:13.5 | Amen.\n", encoding="utf-8")
        _, back, _ = self.roundtrip(edit)
        self.assertEqual(len(back), 3)
        self.assertAlmostEqual(back[2].words[-1].end, 13.5)

    def test_bad_line_gives_clear_error(self):
        with self.assertRaises(CaptionError) as cm:
            self.roundtrip(lambda p: p.write_text("12:43 Lord hear me\n", encoding="utf-8"))
        self.assertIn("MM:SS | text", str(cm.exception))

    def test_missing_file(self):
        with self.assertRaises(CaptionError):
            cp.read_edit_file(Path(tempfile.gettempdir()) / "does_not_exist_zz.txt", "x", LOG)

    def test_previous_copy_is_kept(self):
        pages = self.make_pages()
        with tempfile.TemporaryDirectory() as d:
            txt = Path(d) / "auto-caption.txt"
            cp.write_edit_files(txt, pages, "a", True)
            txt.write_text("00:01.0 | my edit\n", encoding="utf-8")
            cp.write_edit_files(txt, pages, "a", True)
            self.assertEqual((Path(d) / "auto-caption.previous.txt").read_text(encoding="utf-8"), "00:01.0 | my edit\n")


class TimeDecimals(unittest.TestCase):
    def test_whole_seconds_format(self):
        self.assertEqual(cp.fmt_time(763.4, 0), "12:43")
        self.assertEqual(cp.fmt_time(59.6, 0), "01:00")
        self.assertEqual(cp.fmt_time(763.46, 2), "12:43.46")

    def test_untouched_rounded_time_keeps_exact_timing(self):
        """With whole-second times in the file, a line whose time was not edited must not shift."""
        pages = [cp.Page(words=words(("Lord,", 12.4, 12.8), ("hear", 12.9, 13.2), ("me", 13.3, 13.5))),
                 cp.Page(words=words(("Amen.", 20.7, 21.2)))]
        with tempfile.TemporaryDirectory() as d:
            txt = Path(d) / "auto-caption.txt"
            cp.write_edit_files(txt, pages, "fp", False, decimals=0)
            self.assertIn("00:12 | Lord, hear me", txt.read_text(encoding="utf-8"))
            self.assertIn("00:21 | Amen.", txt.read_text(encoding="utf-8"))
            same = cp.read_edit_file(txt, "fp", LOG)
            self.assertAlmostEqual(same[0].words[0].start, 12.4, places=3)
            self.assertAlmostEqual(same[1].words[0].start, 20.7, places=3)
            # text edited, time untouched: still starts at the exact original time
            txt.write_text(txt.read_text(encoding="utf-8").replace("hear me", "hear me out"), encoding="utf-8")
            edited = cp.read_edit_file(txt, "fp", LOG)
            self.assertEqual(edited[0].text, "Lord, hear me out")
            self.assertAlmostEqual(edited[0].words[0].start, 12.4, places=3)
            # time edited: it moves
            txt.write_text(txt.read_text(encoding="utf-8").replace("00:21 | Amen.", "00:23 | Amen."), encoding="utf-8")
            moved = cp.read_edit_file(txt, "fp", LOG)
            self.assertAlmostEqual(moved[1].words[0].start, 23.0, places=3)


class AssBasics(unittest.TestCase):
    def test_colour_alpha_time(self):
        self.assertEqual(ass.color("#4DE3FF"), "&HFFE34D&")  # ASS stores blue-green-red
        self.assertEqual(ass.alpha(1.0), "&H00&")
        self.assertEqual(ass.alpha(0.0), "&HFF&")
        self.assertEqual(ass.timestamp(3725.456), "1:02:05.46")
        with self.assertRaises(ConfigError):
            ass.color("blue")

    def test_escape(self):
        self.assertEqual(ass.escape_text("a{b}\\c\nd"), "a(b)/c d")


class Roles(unittest.TestCase):
    def test_classification_follows_apply_to(self):
        s = settings()
        m = s["mixed_fonts"]
        small, kw = {cs.word_key(x) for x in m["small_words"]}, {cs.word_key(x) for x in m["keywords"]}
        self.assertEqual(cs.classify("Lord's", m, small, kw), "keyword")
        self.assertEqual(cs.classify("LORD,", m, small, kw), "keyword")
        self.assertEqual(cs.classify("the", m, small, kw), "small")
        self.assertEqual(cs.classify("house", m, small, kw), "base")
        only_kw = dict(m, apply_to=["keywords"])
        self.assertEqual(cs.classify("the", only_kw, small, kw), "base")
        self.assertEqual(cs.classify("Jesus", dict(m, enabled=False), small, kw), "base")

    def test_inherit_from_base(self):
        s = settings()
        roles = cs.resolve_roles(s["styles"])
        self.assertEqual(roles["small"]["font"], "Georgia")
        self.assertEqual(roles["small"]["color"], roles["base"]["color"])  # null = same as base
        self.assertEqual(roles["keyword"]["text_transform"], "upper")


class Events(unittest.TestCase):
    def pages(self):
        p = cp.build_pages([words(("Lord,", 5.0, 5.4), ("hear", 5.5, 5.8), ("me", 5.8, 6.0), ("out.", 6.0, 6.5)),
                            words(("Thank", 9.0, 9.4), ("You", 9.4, 9.6), ("Jesus.", 9.6, 10.2)),
                            words(("Amen.", 12.0, 12.6))], captions.DEFAULTS["page"])
        cp.set_display_times(p, captions.DEFAULTS["timing"])
        return p

    def test_two_layers_with_glow_one_without(self):
        s = settings()
        ev, used = cs.build_events(self.pages(), s, 1920, 1080, random.Random(1))
        self.assertEqual(len(ev), 6)
        s["glow"]["enabled"] = False
        ev, _ = cs.build_events(self.pages(), s, 1920, 1080, random.Random(1))
        self.assertEqual(len(ev), 3)

    def test_position_follows_settings(self):
        s = settings()
        s["position"].update(bottom_margin_px=200, y_offset_px=-30)
        s["animation"].update(mode="fixed", fixed="fade")
        ev, _ = cs.build_events(self.pages(), s, 1920, 1080, random.Random(1))
        self.assertIn("\\pos(960,850)", ev[0])  # 1080 - 200 + (-30)

    def test_highlight_glow_and_fonts_present(self):
        s = settings()
        ev, _ = cs.build_events(self.pages(), s, 1920, 1080, random.Random(1))
        text_layer = [e for e in ev if e.startswith("Dialogue: 1,")][0]
        glow_layer = [e for e in ev if e.startswith("Dialogue: 0,")][0]
        self.assertIn("\\1c&HFFE34D&", text_layer)  # highlight colour animates in
        self.assertIn("\\fnSegoe UI Black", text_layer)  # keyword font (Lord)
        self.assertIn("LORD,", text_layer)  # keyword upper-cased
        self.assertIn(f"\\blur{s['glow']['blur']}", glow_layer)  # the configured blur is applied
        self.assertIn("\\1a&HFF&", glow_layer)

    def test_random_animation_never_repeats_back_to_back(self):
        a = captions.DEFAULTS["animation"]
        for seed in range(30):
            seq = cs.choose_animations(40, a, random.Random(seed))
            self.assertTrue(all(x != y for x, y in zip(seq, seq[1:])))
            self.assertTrue(set(seq) <= set(a["pool"]))
        self.assertEqual(len(set(cs.choose_animations(10, dict(a, mode="random_once"), random.Random(3)))), 1)
        self.assertEqual(set(cs.choose_animations(5, dict(a, mode="fixed", fixed="bounce"), random.Random(3))), {"bounce"})

    def test_same_seed_same_animations(self):
        a = captions.DEFAULTS["animation"]
        self.assertEqual(cs.choose_animations(12, a, random.Random(9)), cs.choose_animations(12, a, random.Random(9)))

    def test_preview_window_filters_pages(self):
        s = settings()
        ev, _ = cs.build_events(self.pages(), s, 1920, 1080, random.Random(1), time_shift=8.0, limit=3.0)
        self.assertEqual(len([e for e in ev if e.startswith("Dialogue: 1,")]), 1)  # only the 9.0 s page


class Validation(unittest.TestCase):
    def test_defaults_are_valid(self):
        captions.validate_settings(settings())

    def test_bad_values_are_explained(self):
        for path, value in ((("highlight", "color"), "cyan"), (("glow", "apply_to"), ["everything"]),
                            (("animation", "pool"), ["spin"]), (("position", "y_offset_px"), "up"),
                            (("styles", "base", "size"), 2), (("edit_file", "mode"), "edit")):
            s = settings()
            d = s
            for k in path[:-1]:
                d = d[k]
            d[path[-1]] = value
            with self.assertRaises(ConfigError, msg=str(path)):
                captions.validate_settings(s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
