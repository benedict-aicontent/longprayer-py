"""Unit tests for the FFmpeg filter check (no FFmpeg needed).  Run:  py -3.12 -m unittest discover -s tests -v"""
import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import ffmpeg_tools as ff  # noqa: E402
from core.config_loader import deep_merge  # noqa: E402
from core.errors import ConfigError  # noqa: E402
from features import captions  # noqa: E402

# Lines copied from a real `ffmpeg -filters` run on FFmpeg 9.0.2 (gyan.dev full build): TWO flag characters.
FFMPEG9_ROWS = """\
 TS allpass           A->A       Apply a two-pole all-pass filter.
 .. anull             A->A       Pass the source unchanged to the output.
 TS asuperpass        A->A       Apply high order Butterworth band-pass filter.
 TS bandpass          A->A       Apply a two-pole Butterworth band-pass filter.
 T. virtualbass       A->A       Audio Virtual Bass.
 .. sinc              |->A       Generate a sinc kaiser-windowed low-pass, high-pass, band-pass, or band-reject FIR coefficients.
 .. ass               V->V       Render ASS subtitles onto input video using the libass library.
 TS greyedge          V->V       Estimates scene illumination by grey edge assumption.
 .. null              V->V       Pass the source unchanged to the output.
 .. subtitles         V->V       Render text subtitles onto input video using the libass library.
 .. vidstabdetect     V->V       Extract relative transformations, pass 1 of 2 for stabilization (see vidstabtransform for pass 2).
"""

# The older layout (FFmpeg 6): THREE flag characters, plus the legend FFmpeg prints above the rows.
FFMPEG6_LISTING = """\
Filters:
  T.. = Timeline support
  .S. = Slice threading
  ..C = Command support
  A = Audio input/output
  V = Video input/output
  N = Dynamic number and/or type of input/output
  | = Source or sink filter
 TSC acompressor      A->A       Audio compressor.
 ... anull            A->A       Pass the source unchanged to the output.
 ... ass              V->V       Render ASS subtitles onto input video using the libass library.
 ... subtitles        V->V       Render text subtitles onto input video using the libass library.
 T.. zoompan          V->V       Apply Zoom & Pan effect.
"""


def listing_of_rows(count: int, include: tuple[str, ...] = ()) -> str:
    """A big FFmpeg-9-style listing with `count` made-up filters plus the ones in `include`."""
    rows = [f" .. fake{i:03d}           V->V       Made-up filter number {i}." for i in range(count)]
    rows += [f" .. {name}           V->V       Included filter." for name in include]
    return "\n".join(rows) + "\n"


class FilterNames(unittest.TestCase):
    def test_ffmpeg9_two_flag_rows(self):
        names = ff.filter_names(FFMPEG9_ROWS)
        self.assertIn("ass", names)
        self.assertIn("subtitles", names)
        self.assertIn("allpass", names)
        self.assertEqual(len(names), 11)

    def test_ffmpeg6_three_flag_rows_and_legend(self):
        names = ff.filter_names(FFMPEG6_LISTING)
        self.assertEqual(names, {"acompressor", "anull", "ass", "subtitles", "zoompan"})  # legend lines are not filters

    def test_name_must_match_exactly(self):
        names = ff.filter_names(FFMPEG9_ROWS)
        for wrong in ("pass", "all", "assx", "sub", "ss"):
            self.assertNotIn(wrong, names)


class HasFilter(unittest.TestCase):
    def check(self, help_text, listing, name="ass"):
        """has_filter() with FFmpeg's two answers replaced by the given texts."""
        def fake(cmd):
            return help_text if "-h" in cmd else listing
        with mock.patch.object(ff, "_run_text", side_effect=fake):
            return ff.has_filter("ffmpeg", name)

    def test_present_by_direct_answer(self):
        self.assertIs(self.check("Filter ass\n  Render ASS subtitles onto input video.\n", None), True)

    def test_missing_by_direct_answer(self):
        self.assertIs(self.check("Unknown filter 'ass'.\n", None), False)

    def test_direct_answer_is_not_fooled_by_a_longer_name(self):
        # "Filter assume..." is a different filter; it must not count as "ass" (and cannot decide anything else)
        self.assertIsNot(self.check("Filter assume_something\n  ...\n", None), True)

    def test_unrecognised_answer_falls_back_to_the_listing(self):
        self.assertIs(self.check("???", FFMPEG9_ROWS), True)
        self.assertIs(self.check("???", FFMPEG6_LISTING), True)

    def test_small_unparsed_listing_never_says_missing(self):
        # only 11 rows were understood: not enough evidence to claim a filter is absent
        self.assertIsNone(self.check("???", FFMPEG9_ROWS, name="nosuchfilter"))

    def test_large_listing_without_the_filter_says_missing(self):
        self.assertIs(self.check("???", listing_of_rows(80, include=("subtitles",))), False)
        self.assertIs(self.check("???", listing_of_rows(80, include=("ass",))), True)

    def test_nothing_could_be_run(self):
        self.assertIsNone(self.check(None, None))

    def test_real_run_failure_returns_none(self):
        self.assertIsNone(ff.has_filter("this-program-does-not-exist-xyz", "ass"))


class RequireAssFilter(unittest.TestCase):
    def ctx(self):
        return SimpleNamespace(ffmpeg="C:/ffmpeg/bin/ffmpeg.exe", logger=logging.getLogger("test"))

    def settings(self, check):
        s = deep_merge(captions.DEFAULTS, {}, [])
        s["check_ffmpeg_filter"] = check
        return s

    def test_missing_filter_stops_with_a_helpful_message(self):
        with mock.patch.object(ff, "has_filter", return_value=False):
            with self.assertRaises(ConfigError) as caught:
                captions.require_ass_filter(self.ctx(), self.settings(True))
        message = str(caught.exception)
        self.assertIn("C:/ffmpeg/bin/ffmpeg.exe", message)   # says WHICH ffmpeg was checked
        self.assertIn("-h filter=ass", message)               # how to check by hand
        self.assertIn("check_ffmpeg_filter", message)         # how to bypass a wrong check

    def test_unknown_result_does_not_block(self):
        with mock.patch.object(ff, "has_filter", return_value=None):
            captions.require_ass_filter(self.ctx(), self.settings(True))

    def test_present_filter_passes(self):
        with mock.patch.object(ff, "has_filter", return_value=True):
            captions.require_ass_filter(self.ctx(), self.settings(True))

    def test_bypass_setting_skips_the_check_entirely(self):
        with mock.patch.object(ff, "has_filter", return_value=False) as probe:
            captions.require_ass_filter(self.ctx(), self.settings(False))
        probe.assert_not_called()

    def test_default_is_to_check(self):
        self.assertIs(captions.DEFAULTS["check_ffmpeg_filter"], True)


if __name__ == "__main__":
    unittest.main()
