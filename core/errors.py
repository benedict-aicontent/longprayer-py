"""Custom exceptions shared across the project."""


class ConfigError(Exception):
    """A settings file is missing, malformed, or contains an invalid value."""


class FfmpegError(RuntimeError):
    """An FFmpeg/FFprobe command failed."""


class VoiceoverError(RuntimeError):
    """Voiceover preparation failed (Piper error, unreadable audio, ...)."""


class CaptionError(RuntimeError):
    """Caption preparation failed (speech recognition, unreadable auto-caption.txt, ...)."""


class TextError(RuntimeError):
    """On-screen text preparation failed (an item cannot be read, nothing to show, ...)."""
