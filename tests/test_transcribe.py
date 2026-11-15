"""Speech-recognition input test with a stand-in faster_whisper (needs numpy + ffmpeg on PATH).
Run:  py -3.12 -m unittest discover -s tests -v"""
import logging
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import transcribe  # noqa: E402
from core.errors import CaptionError  # noqa: E402

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

FFMPEG = shutil.which("ffmpeg")
W = {"model": "tiny", "device": "cpu", "compute_type": "int8", "cpu_threads": 0, "model_dir": None,
     "language": "en", "beam_size": 1, "vad_filter": False}


class FakeModel:
    """Behaves like the broken setup for file paths: only arrays are accepted."""
    seen = None

    def __init__(self, *a, **k):
        pass

    def transcribe(self, audio, **k):
        if isinstance(audio, str):
            raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")
        FakeModel.seen = audio
        word = SimpleNamespace(word=" hello", start=0.1, end=0.5)
        seg = SimpleNamespace(words=[word], text="hello", start=0.0, end=0.6)
        return iter([seg]), SimpleNamespace(duration=len(audio) / 16000)


@unittest.skipUnless(np and FFMPEG, "needs numpy and ffmpeg")
class DecodeThenTranscribe(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.wav = self.tmp / "a.wav"
        subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                        "-ar", "44100", "-ac", "2", str(self.wav)], check=True)
        (self.tmp / "temp").mkdir()
        self.ctx = SimpleNamespace(ffmpeg=FFMPEG, logger=logging.getLogger("t"), state={},
                                   paths={"temp": self.tmp / "temp"})
        module = types.ModuleType("faster_whisper")
        module.WhisperModel = FakeModel
        self._old = sys.modules.get("faster_whisper")
        sys.modules["faster_whisper"] = module

    def tearDown(self):
        if self._old is None:
            sys.modules.pop("faster_whisper", None)
        else:
            sys.modules["faster_whisper"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_decoded_array_is_16k_mono_float32(self):
        audio = transcribe._decode_audio(self.ctx, self.wav)
        self.assertEqual(audio.dtype, np.float32)
        self.assertAlmostEqual(len(audio) / 16000, 2.0, delta=0.1)

    def test_transcribe_passes_array_not_path(self):
        words = transcribe.transcribe_words(self.ctx, self.wav, W, "1/1")
        self.assertEqual([x.text for x in words], ["hello"])
        self.assertIsInstance(FakeModel.seen, np.ndarray)

    def test_undecodable_file_gives_clear_error(self):
        bad = self.tmp / "bad.wav"
        bad.write_bytes(b"not audio")
        with self.assertRaises(CaptionError) as caught:
            transcribe.transcribe_words(self.ctx, bad, W, "1/1")
        self.assertIn("could not decode", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
