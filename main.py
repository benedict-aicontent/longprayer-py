"""YouTube long-video automation - entry point.

Examples (Windows, from the project folder):
    py -3.12 main.py --dry-run          plan only, no rendering
    py -3.12 main.py                    build one video
    py -3.12 main.py --duration 120     override the target length (seconds)
    py -3.12 main.py --seed 42          repeat the exact same random choices
    py -3.12 main.py --caption-preview  short test video to tune the caption style (no clips render)
    py -3.12 main.py --text-preview     short test video to tune the on-screen text (settings\\onscreen_text.json)
    py -3.12 main.py --list-fonts       show the font names Windows has (for settings/caption.json)
"""
from __future__ import annotations

import argparse
import random
import secrets
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from core import ffmpeg_tools as ff
from core.config_loader import load_config, resolve_paths
from core.context import Context
from core.errors import CaptionError, ConfigError, FfmpegError, TextError, VoiceoverError
from core.logger import setup_logger
from features import captions, onscreen_text, voiceover
from pipeline import finalize, run_pipeline

ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build a 16:9 1080p YouTube video from clips.")
    p.add_argument("--config", default="config.json", help="config file (default: config.json)")
    p.add_argument("--dry-run", action="store_true", help="plan only; do not render")
    p.add_argument("--duration", type=float, help="target video length in seconds")
    p.add_argument("--seed", type=int, help="random seed (same seed = same result)")
    p.add_argument("--keep-temp", action="store_true", help="keep temp files after the run")
    p.add_argument("--caption-preview", action="store_true",
                   help="render a short caption test video to output\\caption_preview.mp4 and stop")
    p.add_argument("--preview-start", type=float, help="where the preview starts, in seconds of the video "
                   "(default: just before the first caption, or the first text for --text-preview)")
    p.add_argument("--preview-length", type=float, default=12.0, help="preview length in seconds (default 12)")
    p.add_argument("--text-preview", action="store_true",
                   help="render a short on-screen text test video to output\\text_preview.mp4 and stop "
                   "(uses --preview-start / --preview-length too)")
    p.add_argument("--list-fonts", action="store_true", help="print the installed font names and stop")
    return p.parse_args()


def list_fonts() -> int:
    from core.fonts import installed_font_names

    names = installed_font_names()
    if not names:
        print("The installed font list could not be read on this system.")
        return 1
    print("Use these names for \"font\" in settings/caption.json (Windows font names):\n")
    for name in sorted(names, key=str.lower):
        print(f"  {name}")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # a Windows console must never crash on an unusual character
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = parse_args()
    started = time.monotonic()
    if args.list_fonts:
        return list_fonts()

    try:
        config_path = Path(args.config)
        if not config_path.is_absolute():
            config_path = ROOT / config_path
        cfg, warnings = load_config(config_path)
        if args.keep_temp:
            cfg["general"]["keep_temp_files"] = True

        paths = resolve_paths(ROOT, cfg)
        for key in ("clips", "voiceovers", "voices", "script", "music", "output", "temp", "logs"):
            paths[key].mkdir(parents=True, exist_ok=True)

        logger = setup_logger(paths["logs"], cfg["general"]["log_level"])
        for warning in warnings:
            logger.warning(warning)

        ffmpeg = ff.find_binary(cfg["ffmpeg"]["ffmpeg_path"], "FFmpeg")
        ffprobe = ff.find_binary(cfg["ffmpeg"]["ffprobe_path"], "FFprobe")

        seed = args.seed if args.seed is not None else cfg["general"]["random_seed"]
        if seed is None:
            seed = secrets.randbelow(2**31)
        logger.info("Random seed: %d  (use --seed %d to repeat this exact video)", seed, seed)

        work_dir = paths["temp"] / f"run_{datetime.now():%Y%m%d_%H%M%S}"
        ctx = Context(
            root=ROOT, cfg=cfg, paths=paths, logger=logger, rng=random.Random(seed), seed=seed,
            ffmpeg=ffmpeg, ffprobe=ffprobe, work_dir=work_dir, dry_run=args.dry_run,
            config_warnings=[], target_duration=args.duration,
        )

        if args.caption_preview:
            if not cfg["features"].get("voiceover", False):
                raise ConfigError("--caption-preview needs the voiceover feature (features.voiceover = true).")
            voiceover.prepare(ctx)
            preview = captions.preview(ctx, args.preview_start, float(args.preview_length))
            logger.info("Caption preview ready: %s", preview)
            if not cfg["general"]["keep_temp_files"]:
                shutil.rmtree(work_dir, ignore_errors=True)
            return 0

        if args.text_preview:
            preview = onscreen_text.preview(ctx, args.preview_start, float(args.preview_length))
            logger.info("On-screen text preview ready: %s", preview)
            if not cfg["general"]["keep_temp_files"]:
                shutil.rmtree(work_dir, ignore_errors=True)
            return 0

        run_pipeline(ctx)
        if args.dry_run:
            if not cfg["general"]["keep_temp_files"]:
                shutil.rmtree(work_dir, ignore_errors=True)
            return 0

        result = finalize(ctx)
        if result is None:
            logger.error("No video was produced. Is at least one feature enabled in config.json?")
            return 1

        info = ff.probe(ffprobe, result)
        size_mb = result.stat().st_size / (1024 * 1024)
        elapsed = time.monotonic() - started
        logger.info("DONE: %s", result)
        if info:
            logger.info("Length %.1fs | %dx%d | %.1f MB | rendered in %.0fs",
                        info.duration, info.width, info.height, size_mb, elapsed)
        if not cfg["general"]["keep_temp_files"]:
            shutil.rmtree(work_dir, ignore_errors=True)
        return 0

    except ConfigError as exc:
        print(f"\nSETTINGS PROBLEM: {exc}")
        return 2
    except FfmpegError as exc:
        print(f"\nFFMPEG PROBLEM: {exc}")
        return 1
    except VoiceoverError as exc:
        print(f"\nVOICEOVER PROBLEM: {exc}")
        return 1
    except CaptionError as exc:
        print(f"\nCAPTION PROBLEM: {exc}")
        return 1
    except TextError as exc:
        print(f"\nON-SCREEN TEXT PROBLEM: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nStopped by user. Temp files were kept in the temp folder.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
