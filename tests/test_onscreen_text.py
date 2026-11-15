"""Unit tests for the on-screen text logic (no FFmpeg needed).  Run:  py -3.12 -m unittest discover -s tests -v"""
import logging
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config_loader import deep_merge  # noqa: E402
from core.errors import ConfigError  # noqa: E402
from features import onscreen_text as ot  # noqa: E402

LOG = logging.getLogger("test")


def settings(**over):
    s = deep_merge(ot.DEFAULTS, {}, [])
    for key, value in over.items():
        s[key] = deep_merge(s[key], value, []) if isinstance(value, dict) else value
    return s


class Times(unittest.TestCase):
    def test_formats(self):
        for text, expected in (("75", 75), ("1:15", 75), ("4:14", 254), ("1:02:03", 3723),
                               ("1:15.5", 75.5), ("0:05", 5), (" 0:05 ", 5)):
            self.assertAlmostEqual(ot.parse_time(text, "x"), expected, msg=text)

    def test_bad_times(self):
        for text in ("", "abc", "1:75", "1:2:3:4", "-5", "1:60:00", "4:"):
            with self.assertRaises(ConfigError, msg=text):
                ot.parse_time(text, "x")


class Items(unittest.TestCase):
    def test_example_from_the_brief(self):
        items = ot.parse_items(["4:14 | 5:45 | Title"])
        self.assertEqual((items[0].start, items[0].stop, items[0].text), (254, 345, "Title"))

    def test_sorted_and_blank_and_comment_lines_ignored(self):
        items = ot.parse_items(["0:20 | 0:30 | B", "", "# note", "0:05 | 0:10 | A"])
        self.assertEqual([i.text for i in items], ["A", "B"])

    def test_text_may_contain_a_bar_and_newlines(self):
        item = ot.parse_items(["0:01 | 0:09 | Peace | Joy\\nHope"])[0]
        self.assertEqual(item.text, "Peace | Joy\nHope")
        # a JSON "\n" arrives as a real newline: same result
        self.assertEqual(ot.parse_items(["0:01 | 0:09 | Peace\nHope"])[0].text, "Peace\nHope")

    def test_errors_name_the_item(self):
        for rows, needle in ((["0:05 | Title"], "items[1]"),
                             (["0:10 | 0:05 | T"], "later than start"),
                             (["0:01 | 0:05 |   "], "empty"),
                             ([5], "must be a text line"),
                             (["a | 0:05 | T"], "not a time")):
            with self.assertRaises(ConfigError) as caught:
                ot.parse_items(rows)
            self.assertIn(needle, str(caught.exception))
        with self.assertRaises(ConfigError):
            ot.parse_items("0:01 | 0:05 | T")  # a string instead of a list

    def test_fit_to_length(self):
        items = ot.parse_items(["0:05 | 0:10 | in", "0:50 | 1:20 | cut", "2:00 | 2:10 | gone"])
        kept = ot.fit_to_length(items, 60.0, LOG)
        self.assertEqual([(i.text, i.stop) for i in kept], [("in", 10), ("cut", 60.0)])
        self.assertEqual(len(ot.fit_to_length(items, None, LOG)), 3)

    def test_overlaps(self):
        items = ot.parse_items(["0:00 | 0:10 | a", "0:05 | 0:15 | b", "0:15 | 0:20 | c"])
        self.assertEqual([(a.text, b.text) for a, b in ot.overlapping(items)], [("a", "b")])


class Settings(unittest.TestCase):
    def test_defaults_are_valid(self):
        ot.validate_settings(settings())

    def test_bad_values_are_explained(self):
        for over, needle in ((dict(position={"anchor": "left"}), "position.anchor"),
                             (dict(style={"color": "red"}), "style.color"),
                             (dict(animation={"mode": "often"}), "animation.mode"),
                             (dict(animation={"pool": ["spin"]}), "animation.pool"),
                             (dict(items=["oops"]), "items[1]")):
            with self.assertRaises(ConfigError) as caught:
                ot.validate_settings(settings(**over))
            self.assertIn(needle, str(caught.exception))

    def test_default_is_top_center_and_random_once(self):
        d = ot.DEFAULTS
        self.assertEqual(d["position"]["anchor"], "top_center")
        self.assertEqual(d["animation"]["mode"], "random_once")


class Layout(unittest.TestCase):
    def test_anchor_points_and_offsets(self):
        self.assertEqual(ot.anchor_point(settings(), 1920, 1080), (8, 960, 90))
        s = settings(position={"x_offset_px": 30, "y_offset_px": -20})
        self.assertEqual(ot.anchor_point(s, 1920, 1080), (8, 990, 70))
        self.assertEqual(ot.anchor_point(settings(position={"anchor": "bottom_right"}), 1920, 1080), (3, 1800, 990))
        self.assertEqual(ot.anchor_point(settings(position={"anchor": "center"}), 1920, 1080), (5, 960, 540))


class Animations(unittest.TestCase):
    def test_one_animation_for_the_whole_video(self):
        a = settings()["animation"]
        for seed in range(10):
            chosen = ot.choose_animations(5, a, random.Random(seed))
            self.assertEqual(len(set(chosen)), 1)
            self.assertIn(chosen[0], a["pool"])

    def test_random_per_video(self):
        a = settings()["animation"]
        picked = {ot.choose_animations(1, a, random.Random(seed))[0] for seed in range(60)}
        self.assertGreater(len(picked), 2)

    def test_fixed_and_per_item(self):
        a = settings(animation={"mode": "fixed", "fixed": "drop"})["animation"]
        self.assertEqual(ot.choose_animations(3, a, random.Random(1)), ["drop"] * 3)
        a = settings(animation={"mode": "random_per_item"})["animation"]
        self.assertEqual(len(ot.choose_animations(8, a, random.Random(1))), 8)


class Events(unittest.TestCase):
    def build(self, rows, **over):
        s = settings(**over)
        return ot.build_events(ot.parse_items(rows), s, 1920, 1080, random.Random(1))

    def test_layers_follow_the_settings(self):
        events, used = self.build(["0:01 | 0:05 | Hello"], box={"enabled": True})
        self.assertEqual([e.split(",")[0] for e in events], ["Dialogue: 0", "Dialogue: 1", "Dialogue: 2"])
        self.assertIn(",Box,", events[0])
        self.assertEqual(sum(used.values()), 1)
        events, _ = self.build(["0:01 | 0:05 | Hello"], glow={"enabled": False})
        self.assertEqual(len(events), 1)

    def test_times_text_position(self):
        events, _ = self.build(["1:05 | 1:10.5 | Hello\\nWorld"], animation={"mode": "fixed", "fixed": "fade"})
        text = events[-1]
        self.assertIn("0:01:05.00,0:01:10.50", text)
        self.assertIn("Hello\\NWorld", text)
        self.assertIn("\\an8\\pos(960,90)", text)

    def test_text_transform_and_escaping(self):
        events, _ = self.build(["0:01 | 0:05 | a {b}"], style={"text_transform": "upper"})
        self.assertIn("A (B)", events[-1])

    def test_short_item_shrinks_fades(self):
        events, _ = self.build(["0:01 | 0:01.4 | Hi"], animation={"mode": "fixed", "fixed": "fade"})
        self.assertIn("\\fad(200,200)", events[-1])

    def test_reveal_is_word_by_word(self):
        events, _ = self.build(["0:01 | 0:09 | one two three"], animation={"mode": "fixed", "fixed": "reveal"})
        self.assertEqual(events[-1].count("}one") + events[-1].count("}two ") + events[-1].count("}three"), 3)

    def test_preview_shift_and_limit(self):
        s = settings()
        items = ot.parse_items(["0:10 | 0:20 | a", "0:40 | 0:50 | b"])
        events, used = ot.build_events(items, s, 1920, 1080, random.Random(1), time_shift=8.0, limit=15.0)
        self.assertEqual(sum(used.values()), 1)
        self.assertIn("0:00:02.00,0:00:12.00", events[-1])

    def test_each_animation_builds(self):
        for name in ot.ANIMATIONS:
            events, used = self.build(["0:01 | 0:06 | Hello there"], animation={"mode": "fixed", "fixed": name})
            self.assertEqual(used, {name: 1})


if __name__ == "__main__":
    unittest.main()
