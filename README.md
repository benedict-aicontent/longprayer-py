# TheFaithFuel Long Video Automation

Builds one 16:9, 1080p, H264 `.mp4` for YouTube from the clips in `/clips`.
Features are added one at a time. **Done: Feature 1 (combine clips), Feature 2 (voiceover), Feature 3 (background music), Feature 4 (auto-captions) and Feature 5 (on-screen text).**

| # | Feature | Status |
|---|---------|--------|
| 1 | Combine video clips | Done, tested |
| 2 | Voiceover (recorded files or script + Piper) | Done, tested (Piper tested with a stand-in, see limitations) |
| 3 | Background music (loop, fades, ducking) | Done, tested |
| 4 | Auto-captions (glow, mixed fonts, animation, editable text file) | Done, tested (real faster-whisper not tested, see limitations) |
| 5 | On-screen text (timed texts, glow, one animation per video) | Done, tested on Linux (see limitations) |
| 6 | Motion objects | Not built |

## Requirements

* Windows 11, **Python 3.12** (recommended; 3.15 is too new for future libraries)
* FFmpeg + FFprobe on PATH (`ffmpeg -version` must work). Captions also need an FFmpeg build with libass: `ffmpeg -hide_banner -h filter=ass` must print `Filter ass`
* Features 1 to 3 need **no pip packages** (standard library only)
* Feature 4 with `source: "faster_whisper"` (the default) needs one package:  `py -3.12 -m pip install faster-whisper`  (the first run also downloads the speech model, so it needs internet once)
* Feature 2 script mode needs Piper TTS and at least one voice (`.onnx` + `.onnx.json`) in `voices\`

## Folder structure

```
thefaithfuel-longprayer-py\
  main.py              entry point
  pipeline.py          runs the enabled features in order, names the output file
  config.json          GENERAL settings (paths, video/audio quality, seed, logging)
  settings\
    clips.json         Feature 1 settings
    voiceover.json     Feature 2 settings
    music.json         Feature 3 settings
    caption.json       Feature 4 settings
    onscreen_text.json Feature 5 settings and your list of texts
  core\                config loader, logger, FFmpeg helpers, subtitle (ASS) writer, speech recognition, font check
  tests\               unit tests (captions, on-screen text, FFmpeg filter check, speech input)
  features\
    clips.py           Feature 1
    voiceover.py       Feature 2
    music.py           Feature 3
    captions.py        Feature 4 (+ caption_pages.py, caption_style.py)
    onscreen_text.py   Feature 5
  clips\               your 16:9 clips go here
  voiceovers\          recorded voiceover files (Feature 2)
  script\              script .txt file(s) for Piper (Feature 2)
  voices\              Piper voices: name.onnx + name.onnx.json (Feature 2)
  music\               background music files, mp3 / wav (Feature 3)
  auto-caption.txt     the captions as text, to correct spelling/timing (created by Feature 4)
  output\              finished videos: video_001.mp4, video_002.mp4 ...
  temp\                working files, deleted after a successful run
  logs\                run_*.log and ffmpeg_*.log (check these when something fails)
```

## How to run

Command Prompt:

```
cd /d B:\Automation\python\thefaithfuel-longprayer-py
py -3.12 main.py --dry-run
py -3.12 main.py
```

PowerShell (the same, but `cd` has no `/d`):

```
cd B:\Automation\python\thefaithfuel-longprayer-py
py -3.12 main.py --dry-run
py -3.12 main.py
```

Note for PowerShell: `where` is a PowerShell command of its own and prints nothing useful. To find a program use `where.exe ffmpeg` or `Get-Command ffmpeg`.

| Option | Meaning |
|--------|---------|
| `--dry-run` | Shows the plan (which clip, which trim, which transition). Renders nothing. Use it to tune settings quickly. |
| `--duration 120` | Target length in seconds. Only used when `features.voiceover` is `false`. |
| `--seed 42` | Repeats the exact same random choices. The seed is printed on every run. |
| `--keep-temp` | Keeps the temp files (for debugging). |
| `--config other.json` | Use a different general config file. |
| `--caption-preview` | Renders a short test video (your first clip + the voiceover + the captions) to `output\caption_preview.mp4` and stops. Use it to tune the caption style quickly. |
| `--preview-start 20` / `--preview-length 12` | Where the preview starts (seconds of the video; default just before the first caption) and how long it is. |
| `--list-fonts` | Prints the font names Windows has. Use these exact names in `settings\caption.json`. |

## What Feature 1 does

1. Reads every clip in `clips\` (unreadable files and clips too short for the transition are skipped with a warning).
2. Plans random trims between `min_seconds` and `max_seconds`. When all clips have been used it loops (reshuffles) until the target length is covered.
3. Picks **one** transition type for the whole video (random from your pool, or a fixed one).
4. Joins everything with FFmpeg. Long plans are rendered in batches (`join.batch_size`) so an 8 GB PC never holds too many clips at once, then the batches are joined in a final pass.
5. Saves `output\video_NNN.mp4` (1920x1080, 30 fps, H264 + AAC 192k 48 kHz, `faststart`).

The target length is `target_duration_seconds`, but when the voiceover feature is on, the voiceover decides the length (see below) and `--duration` is ignored.

## What Feature 2 does

1. **Source** (`source` in voiceover.json): `auto` uses the audio files in `voiceovers\` if there are any, otherwise the `.txt` script in `script\`. Force one with `recorded` or `script`.
2. **Recorded files**: played in filename order. Numbers sort naturally, so `1_`, `2_`, `10_` is the right order.
3. **Script**: every `.txt` in `script\` (filename order) is split into paragraphs at blank lines. Each paragraph is spoken by Piper as one voiceover. A blank line is a pause: one blank line = `between_seconds`, and with `multiply_pause_by_blank_lines` on, two blank lines = twice that, and so on. Lines inside a paragraph are joined into one sentence flow.
4. **Track**: `lead_seconds` of silence, voice 1, pause, voice 2, ..., last voice, `tail_seconds` of silence.
5. **The voiceover decides the video length** (lead + speech + pauses + tail). The clips are then planned to exactly that length and the voiceover is laid over the finished video.
6. **Speed** (`speed`) is applied to recorded and Piper audio the same way (0.5 to 2.0, pitch is kept). Pauses, lead and tail are NOT changed by speed.
7. **Loudness**: every voiceover is normalized so the whole track measures `target_lufs` (YouTube plays around -14 LUFS; -16 is a good voice target). `volume_db` then adds or removes extra gain.
8. If the clips keep their own audio (`tracks_muted: false`), the voiceover is added **on top of it**. Use `clip_audio_volume` in `clips.json` to turn the clip sound down.

Piper results are cached in `temp\tts_cache\` (same text + same voice = no new TTS run), so re-running is fast.

## settings/voiceover.json (Feature 2)

| Key | Default | Notes |
|-----|---------|-------|
| `source` | `"auto"` | `auto`, `recorded` or `script`. |
| `speed` | `1.0` | One speed for recorded and Piper audio. 1.1 = 10% faster. |
| `volume_db` | `0.0` | Extra gain after loudness normalization. |
| `pauses.between_seconds` | `1.0` | Pause between voiceovers (one blank line in the script). |
| `pauses.lead_seconds` | `5.0` | Silence before the first voiceover. |
| `pauses.tail_seconds` | `20.0` | Video continues this long after the last voiceover. |
| `recorded.extensions` / `order` | mp3, wav, m4a, flac, ogg, aac, opus / `natural` | `natural` or `alphabetical`. |
| `script.extensions` | `[".txt"]` | |
| `script.multiply_pause_by_blank_lines` | `true` | `false` = always one pause, however many blank lines. |
| `piper.command` | `["py", "-3.12", "-m", "piper"]` | How to start Piper. For Piper installed with `pip install piper-tts` (version 1.x) use the default. For the old standalone `piper.exe` use its real full path, for example `["D:/tools/piper/piper.exe"]` (forward slashes). |
| `piper.model_flag` / `output_flag` | `"-m"` / `"-f"` | Piper option names (these are the documented ones for piper-tts 1.x). |
| `piper.text_mode` | `"argument"` | How the text is given to Piper. `argument` = after `--` on the command line (piper-tts 1.x, `pip install piper-tts`). `stdin` = piped in (the old standalone `piper.exe`). |
| `piper.voice` | `null` | `null` = first `.onnx` in `voices\`. Or a name such as `"en_US-lessac-medium"`, or a full path. |
| `piper.extra_args` | `[]` | Extra Piper options exactly as your version spells them (see `py -3.12 -m piper --help`), for example `["--data-dir", "B:/path/to/voices"]` if Piper cannot find a voice by itself. |
| `piper.use_cache` / `timeout_seconds` | `true` / `300` | |
| `normalize.mode` | `"whole_track"` | `whole_track` (levels the final track), `per_file` (levels each file separately, good if your recordings differ in volume), `off`. |
| `normalize.target_lufs` / `true_peak_db` / `loudness_range` | `-16` / `-1.5` / `11` | |

`--dry-run` also builds the voiceover (it needs the real length), so with a script it runs Piper once per new paragraph.

## What Feature 3 does

1. **Picks one music file** from `music\` at random (`file: null`), or the file you name in `music.json`. The same file is used for the whole video.
2. **Normalizes it** to the same reference loudness as the voiceover (-16 LUFS), so the dB levels below mean the same thing for every song.
3. **Loops it** with a crossfade (`loop.crossfade_seconds`) if it is shorter than the video. A longer file is simply cut. Only the part that is needed is processed, so a 1-hour file is fine.
4. **Fades** in at the start and out at the very end of the video.
5. **Starts** at `start_at_seconds` (0.0 = from the first second).
6. **Sets the level**: `no_voice_db` normally (lead, tail), `under_voice_db` while the voiceover speaks. The music starts lowering `attack_seconds` BEFORE the voice starts and rises `release_seconds` after it ends. Gaps between voiceovers shorter than `keep_ducked_gaps_shorter_than_seconds` stay ducked, so the music does not pump up and down between paragraphs.
7. **Mixes it in** on top of the voiceover and the clip audio. The music never lowers the clip audio (they are separate).

**Levels are relative to the voiceover.** With the defaults the voice is at -16 LUFS, the music is at -28 LUFS in the lead and tail (`-12`) and at -38 LUFS under the voice (`-22`). If the lead/tail music is too quiet for your taste, raise `no_voice_db` (for example to `-6`).

If the voiceover feature is off, there is nothing to duck, so the music stays at `no_voice_db` for the whole video.

## settings/music.json (Feature 3)

| Key | Default | Notes |
|-----|---------|-------|
| `extensions` | `[".mp3", ".wav"]` | Add more here later, e.g. `".m4a"`, `".flac"`. |
| `file` | `null` | `null` = random pick each run. Or a name such as `"calm.mp3"` (or `"calm"`). |
| `start_at_seconds` | `0.0` | When the music begins. |
| `levels.under_voice_db` | `-22.0` | Music level while the voice speaks (dB relative to the voice). |
| `levels.no_voice_db` | `-12.0` | Music level when no one speaks (lead, tail, long pauses). |
| `ducking.enabled` | `true` | `false` = always `no_voice_db`. |
| `ducking.attack_seconds` / `release_seconds` | `0.5` / `1.0` | How fast the music goes down / comes back. |
| `ducking.keep_ducked_gaps_shorter_than_seconds` | `3.0` | Pauses shorter than this do not bring the music back up. Never less than attack + release. |
| `fade.in_seconds` / `out_seconds` | `3.0` / `8.0` | Shortened automatically for very short videos. |
| `loop.crossfade_seconds` | `2.0` | `0` = hard restart. Reduced automatically for very short music files. |
| `normalize.enabled` | `true` | `false` = use the file's own loudness (the dB levels are then relative to that). |
| `normalize.target_lufs` / `true_peak_db` / `loudness_range` | `-16` / `-1.5` / `11` | Keep `target_lufs` equal to the voiceover's so the levels stay relative to the voice. |

## What Feature 4 does

Captions need the voiceover feature (they follow the spoken words).

1. **Words and timing** (`source`):
   * `faster_whisper` (default): speech recognition finds every spoken word and its time. If the voiceover came from the `script\` folder and `use_script_text` is `true`, the captions show the **script's own spelling and punctuation** and recognition only supplies the timing.
   * `script`: no recognition at all. The script's words are spread over each paragraph's speech (needs a script voiceover; less precise, nothing to install).
2. **Lines**: one full line at a time (up to `page.max_words` words / `max_chars` characters). Lines end at sentence ends and long silences. A sentence that is too long is cut into similar-sized lines, preferably at commas. A line never crosses from one voiceover file to the next.
3. **Only while the voice speaks**: nothing is shown in the lead, the tail or the pauses between voiceovers.
4. **Active word**: the word being spoken is highlighted (`highlight`) with a glow (`glow`), and goes back to normal after it is spoken (`highlight.after_spoken`).
5. **Mixed fonts**: three roles. `base` (every word), `small` (little words such as "the", "to", "of"; italic serif by default) and `keyword` (words such as "Lord", "Jesus", "grace"; heavy italic UPPERCASE by default). `mixed_fonts.apply_to` chooses which roles are used, `mixed_fonts.small_words` and `keywords` are your word lists, and `enabled: false` turns mixed fonts off.
6. **Animation**: every line gets a random animation from `animation.pool` (never the same one twice in a row). Names: `fade`, `pop`, `bounce`, `slide_up`, `drop`, `blur_in`, `reveal` (word by word). Use `mode: "fixed"` for one animation, or `"random_once"` for one random animation for the whole video.
7. **Position**: bottom-center. `position.y_offset_px` moves it (negative = up, positive = down). It is always centered left-to-right.
8. **Burned in during the final clip render**, so there is no extra video encode.

### Correcting the captions (auto-caption.txt)

Every run (in `generate` mode) writes `auto-caption.txt` in the project folder, one caption line per row:

```
00:05.2 | Heavenly Father, we come to
00:07.1 | You today with grateful hearts.
00:09.6 | Thank You for Your grace,
```

1. Open `auto-caption.txt` and fix spelling and/or the time of a line. The time is when the line's first word is spoken (`12:43` or `12:43.5`). Optional end time: `12:43.5 - 12:47.0 | text`.
2. Set `"edit_file": { "mode": "use_edited" }` in `settings\caption.json`.
3. Run again. In `use_edited` mode no speech recognition runs, so it is quick, and your text and times are used.
4. If you only change the **text**, the words are re-timed across the same line. If you only change the **time**, the line moves as a whole. Untouched lines keep their exact original timing, even if the file shows them rounded.
5. Before a new `generate` run overwrites the file, the old one is saved as `auto-caption.previous.txt` (`edit_file.keep_previous`). `auto-caption.json` next to it holds the exact word timing; do not edit or delete it (the txt still works without it, only with less precise timing).

The txt shows times with one decimal (`00:12.3`). Set `edit_file.time_decimals` to `0` for `00:12`, or `2` for hundredths.

### Tuning the look quickly

```
py -3.12 main.py --caption-preview
py -3.12 main.py --caption-preview --preview-start 30 --preview-length 10
```

The preview takes seconds (it does not render your whole video). Speech recognition results are cached in `temp\caption_cache\`, so repeated previews do not run recognition again.

## What Feature 5 does

Shows texts on the video for the times you choose (titles, headings, a verse reference, "Subscribe" ...).

1. **Your list** is `items` in `settings\onscreen_text.json`, one text per line as `"start | stop | text"`:

   ```json
   "items": [
     "0:05 | 0:14 | Praying for Peace",
     "4:14 | 5:45 | Amen",
     "1:02:10 | 1:02:20 | Thank you for watching\nSee you next time"
   ]
   ```

   * Times: seconds (`75`), `M:SS` (`1:15`), `H:MM:SS` (`1:02:03`), decimals allowed (`1:15.5`).
   * `\n` inside the text starts a new line. Lines that start with `#` and blank lines are ignored.
   * Stop must be later than start. An item that starts after the video ends is skipped (with a warning); one that runs past the end is cut at the end. Two items that overlap in time are drawn on top of each other (a warning tells you).
2. **Position**: top-center by default. `position.anchor` can be `top_left`, `top_center`, `top_right`, `middle_left`, `center`, `middle_right`, `bottom_left`, `bottom_center`, `bottom_right`. `x_offset_px` / `y_offset_px` move it (x: positive = right, y: positive = down). Captions are at the bottom, so the default does not collide with them.
3. **Look**: `style` (font, size, bold, italic, color, opacity, upper/lower/title case, letter spacing, outline, shadow), an optional background `box`, and a `glow` (on by default, same cyan as the captions).
4. **Animation**: ONE animation for all texts of a video, picked at random for every new video (`animation.mode: "random_once"`, the default). Use `"fixed"` with `animation.fixed` to always use the same one, or `"random_per_item"` for a different one per text. Names: `fade`, `pop`, `bounce`, `slide_up`, `drop`, `blur_in`, `reveal` (word by word). `animation.pool` is the list the random pick uses.
5. **Burned in during the final clip render**, together with the captions, so there is no extra video encode. Texts are drawn above the captions.

Turn it off with `features.onscreen_text: false` in `config.json`. With an empty `items` list nothing is shown.

### Tuning the look quickly

```
py -3.12 main.py --text-preview
py -3.12 main.py --text-preview --preview-start 250 --preview-length 10
```

This renders a short silent test video (`output\text_preview.mp4`: your first clip with the texts on it). It does not need the voiceover. By default it starts one second before your first text.

## settings/onscreen_text.json (Feature 5)

| Key | Default | Notes |
|-----|---------|-------|
| `items` | `[]` | Your texts, see above. |
| `check_ffmpeg_filter` | `true` | Same FFmpeg `ass` filter check as the captions. |
| `position.anchor` | `"top_center"` | See above. |
| `position.margin_x_px` / `margin_y_px` | `120` / `90` | Distance from the edge the anchor is on (left/right and top/bottom). Ignored for the center directions. |
| `position.x_offset_px` / `y_offset_px` | `0` / `0` | Fine adjustment. |
| `position.side_margin_px` | `160` | Long texts wrap inside this left/right margin. |
| `style.*` | Segoe UI, 72, bold, white | `font`, `size`, `bold`, `italic`, `color`, `opacity`, `text_transform` (`none`/`upper`/`lower`/`title`), `letter_spacing_px`, `outline_px`, `outline_color`, `shadow_px`, `shadow_color`, `shadow_opacity`. |
| `box.enabled` / `color` / `opacity` / `padding_px` | `false` / `"#000000"` / `0.55` / `22` | A filled box behind the text. With several lines the boxes of the lines overlap slightly, which looks a little darker where they touch. |
| `glow.enabled` / `color` / `size_px` / `blur` / `opacity` | `true` / `"#4DE3FF"` / `6` / `12` / `0.85` | |
| `animation.mode` | `"random_once"` | `random_once`, `fixed`, `random_per_item`. |
| `animation.fixed` / `pool` | `"fade"` / 6 names | |
| `animation.in_ms` / `out_ms` | `450` / `450` | Fade/move time in and out. Shrunk automatically for very short texts. |
| `animation.reveal_word_ms` | `160` | Delay between words for `reveal`. |

Fonts: same rules as the captions (`py -3.12 main.py --list-fonts`).

## settings/caption.json (Feature 4)

| Key | Default | Notes |
|-----|---------|-------|
| `source` | `"faster_whisper"` | `faster_whisper` or `script`. |
| `use_script_text` | `true` | Script voiceover only: show the script's spelling, take the timing from recognition. |
| `check_ffmpeg_filter` | `true` | Before a caption render, the app asks FFmpeg whether it has the `ass` filter (libass). Set to `false` to skip that check if it is ever wrong for your FFmpeg. |
| `whisper.model` | `"small"` | `tiny`, `base`, `small`, `medium`. Smaller = faster and less accurate. If it is too slow on your PC, try `base`. |
| `whisper.device` / `compute_type` | `"cpu"` / `"int8"` | Right for a PC without a GPU. |
| `whisper.language` | `"en"` | `"auto"` to detect. |
| `whisper.beam_size` / `vad_filter` / `cpu_threads` / `model_dir` | `5` / `false` / `0` / `null` | `model_dir` = folder to keep the downloaded model in. |
| `edit_file.path` | `"auto-caption.txt"` | Relative to the project folder, or a full path. |
| `edit_file.mode` | `"generate"` | `generate` (build the file) or `use_edited` (read your corrections). |
| `edit_file.keep_previous` / `time_decimals` | `true` / `1` | See above. |
| `page.max_words` / `max_chars` | `8` / `56` | Size of one caption line. Longer sentences are split. |
| `page.max_gap_seconds` | `0.8` | A silence longer than this ends a line. |
| `page.break_after_sentence` / `prefer_breaks_at_commas` | `true` / `true` | |
| `page.merge_short_sentences_up_to_words` | `2` | A very short sentence ("Amen.") joins the line before it if it fits. `0` = never. |
| `timing.lead_in_seconds` / `hold_after_seconds` | `0.08` / `0.25` | A line appears slightly before its first word and stays slightly after its last. |
| `timing.min_page_seconds` / `time_offset_seconds` | `0.6` / `0.0` | `time_offset_seconds` shifts ALL captions (positive = later) if they feel early or late. |
| `position.bottom_margin_px` | `120` | Distance from the bottom edge of the video to the bottom of the text. |
| `position.y_offset_px` | `0` | Moves the captions up (negative) or down (positive). |
| `position.side_margin_px` | `160` | Left/right margin; lines wrap inside it. |
| `styles.base` | Segoe UI, 88, bold, white | `font`, `size`, `bold`, `italic`, `color`, `opacity`, `text_transform` (`none`/`upper`/`lower`/`title`), `letter_spacing_px`, `outline_px`, `outline_color`, `shadow_px`, `shadow_color`, `shadow_opacity`. |
| `styles.small` / `styles.keyword` | Georgia 62 italic / Segoe UI Black 98 italic UPPER | Any key set to `null` means "same as `base`". |
| `mixed_fonts.enabled` / `apply_to` | `true` / `["small_words", "keywords"]` | `apply_to` may hold `"small_words"`, `"keywords"` or both. |
| `mixed_fonts.small_words` / `keywords` | lists | Edit the lists freely. Matching ignores capital letters and punctuation. |
| `highlight.enabled` / `color` | `true` / `"#4DE3FF"` | Color of the word being spoken. |
| `highlight.after_spoken` | `"base"` | `base` (back to normal), `keep` (stay highlighted), `dim`. |
| `highlight.dim_opacity` / `transition_ms` / `bridge_gaps_under_seconds` | `0.6` / `70` / `0.3` | |
| `glow.enabled` / `color` / `size_px` / `blur` / `opacity` | `true` / `"#4DE3FF"` / `8` / `14` / `0.9` | |
| `glow.apply_to` | `["active_word"]` | May hold `"active_word"`, `"keywords"` (always glowing) and `"all"`. |
| `animation.mode` | `"random_per_line"` | `random_per_line`, `random_once`, `fixed`. |
| `animation.fixed` / `pool` | `"pop"` / 6 names | `pool` may use any of the 7 names above. |
| `animation.in_ms` / `out_ms` | `220` / `150` | Speed of the in and out animation. |

**Fonts**: use the Windows font name, for example `"Segoe UI"`, `"Georgia"`, `"Montserrat"`. Run `py -3.12 main.py --list-fonts` to see what your PC has. A name that is not installed gives a warning, and Windows then substitutes another font. A font family with a weight in its name, such as `"Segoe UI Black"`, is its own font name. For a bold look on a normal family, use `bold: true`.

## config.json (general)

| Key | Default | Notes |
|-----|---------|-------|
| `general.random_seed` | `null` | A number = same result every run. `null` = new random every run. |
| `general.keep_temp_files` | `false` | |
| `general.log_level` | `"INFO"` | `DEBUG` shows the full FFmpeg commands on screen. |
| `features.voiceover` / `music` / `captions` / `onscreen_text` / `clips` | `true` | Switch a feature on/off. With `voiceover` off, the video length comes from `clips.json`. |
| `paths.*` | folders above (`paths.voices` = Piper voices folder, change it here) | Relative to the project, or absolute. Use `B:/folder` or `B:\\folder` in JSON. |
| `ffmpeg.ffmpeg_path` / `ffprobe_path` | `"ffmpeg"` / `"ffprobe"` | Full path if not on PATH. |
| `ffmpeg.threads` | `0` | 0 = automatic. Lower it (e.g. 4) if the PC becomes unresponsive. |
| `video.width/height/fps` | 1920 / 1080 / 30 | Must be even numbers. |
| `video.preset` | `"veryfast"` | `ultrafast` = fastest, bigger file. `medium` = slower, smaller file. |
| `video.crf` | `20` | Quality. Lower = better and bigger (18 to 23 is the normal range). |
| `audio.bitrate` / `sample_rate` | `192k` / `48000` | |
| `output.name_prefix` / `digits` | `video_` / `3` | Output name `video_001.mp4`. Uses the highest existing number + 1. |

## settings/clips.json (Feature 1)

| Key | Default | Notes |
|-----|---------|-------|
| `target_duration_seconds` | `180` | Final length. |
| `extensions` | mp4, mov, mkv, avi, webm, m4v | |
| `order` | `"random"` | `random` or `alphabetical`. |
| `when_exhausted` | `"reshuffle"` | Looping: `reshuffle` (new order and new random trims each round) or `repeat_in_order`. |
| `avoid_back_to_back_repeat` | `true` | Never the same clip twice in a row when looping. |
| `clip_duration.min_seconds` / `max_seconds` | `4.0` / `8.0` | Each trim is a random length in this range. |
| `trim_position` | `"random"` | `random` = random start inside the clip. `start` = always from the beginning. |
| `short_clip_policy` | `"use_whole"` | Clip shorter than the chosen length: `use_whole` or `skip`. |
| `fit_mode` | `"fill"` | Non-16:9 clips: `fill` = crop to fill, `fit` = black bars. |
| `tracks_muted` | `false` | `true` = output has no audio from the clips. |
| `clip_audio_volume` | `1.0` | Volume of the clips' own audio (when not muted). |
| `transition.enabled` | `true` | `false` = hard cuts. |
| `transition.mode` | `"random_once"` | `random_once` = one random type for the whole video. `fixed` = use `fixed_type`. |
| `transition.duration_seconds` | `0.8` | Rounded to a whole number of frames. `min_seconds` must be at least 2x this + 0.2. |
| `transition.random_pool` | 12 calm types | Any FFmpeg `xfade` name, e.g. fade, dissolve, fadeblack, wipeleft, slideleft, circleopen, radial, zoomin, pixelize. Names your FFmpeg lacks are skipped with a warning. |
| `join.batch_size` | `8` | Clips rendered per batch. Lower = less RAM, higher = fewer passes. |
| `join.intermediate_preset` / `intermediate_crf` | `superfast` / `18` | Quality of the temporary batch files. |

Keys starting with `_` are ignored, so you can leave notes in the JSON files. Misspelled keys are reported as warnings.

## Troubleshooting

* **`FFmpeg not found`**: add FFmpeg's `bin` folder to PATH or set `ffmpeg.ffmpeg_path` in `config.json`.
* **`FFmpeg (...) reports that it has no 'ass' filter`**: check by hand with  `ffmpeg -hide_banner -h filter=ass`. It must print `Filter ass`. If it prints `Unknown filter`, use a full FFmpeg build (for example from gyan.dev) and set `ffmpeg.ffmpeg_path` and `ffmpeg.ffprobe_path` in `config.json`. If it prints `Filter ass` but the app still stops, set `check_ffmpeg_filter` to `false` in `settings\caption.json` and tell me. (Older versions of this app had a bug that wrongly stopped on FFmpeg 9.)
* **`items[2]: ... must look like "start | stop | text"`** (or `... is not a time`): a line in `items` in `settings\onscreen_text.json` is wrong. The message names the line number. Every line needs two times and a text separated by `|`.
* **On-screen text does not appear**: check `features.onscreen_text` in `config.json`, that `items` is not empty, and the log for "starts ... after the video ends". Use `--text-preview` to see the look.
* **`SETTINGS PROBLEM`**: the message names the setting and the allowed values.
* **`FFMPEG PROBLEM`**: the last lines of FFmpeg's error are printed; the full log is in `logs\ffmpeg_*.log`.
* **`faster-whisper is not installed`**: run `py -3.12 -m pip install faster-whisper`. For a script voiceover you can instead set `source` to `"script"` in `settings\caption.json` (no install).
* **`Speech recognition failed ... unexpected keyword argument 'metadata_errors'`**: faster-whisper's own audio reader (PyAV) was incompatible. This app now decodes the audio with your FFmpeg and hands faster-whisper the samples, so PyAV is no longer used. If you still see it, run `py -3.12 -m pip install -U faster-whisper av` and send me the full message.
* **`Could not load the Whisper model`**: the first run needs internet to download the model once. Set `whisper.model_dir` to keep it in a folder you choose.
* **Captions look wrong, are in the wrong font, or too big**: run `--caption-preview`, then change `settings\caption.json`. Check the font names with `--list-fonts`.
* **Captions early or late**: change `timing.time_offset_seconds`, or correct single lines in `auto-caption.txt`.
* **`CAPTION PROBLEM ... expected 'MM:SS | text'`**: a line in `auto-caption.txt` has no time. Every line needs `minutes:seconds | text` (lines starting with `#` are ignored).
* **`No music files ... found`**: put an mp3 or wav in `music\`, or set `features.music` to `false` in `config.json`.
* **Music too loud or too quiet**: change `levels` in `settings\music.json` (see above).
* **`Piper command ... was not found`**: set `piper.command` in `settings\voiceover.json` (see the table above).
* **`Piper failed`**: the message shows Piper's own error and the exact command. Copy that command into Command Prompt to see the problem directly. A manual test that should work:  `py -3.12 -m piper -m voices\YOURVOICE.onnx -f test.wav -- "This is a test."`. If Piper rejects an option, send me the output of `py -3.12 -m piper --help`.
* **Slow or PC freezing**: set `video.preset` to `ultrafast` or `ffmpeg.threads` to 4 while testing.
* **Disk space**: batched mode writes temporary batch files into `temp\` (deleted after a successful run). Keep several GB free for a 10-minute video.

## Known limitations

* Tested on a different machine (Linux, FFmpeg 6.1, synthetic clips and tones), not on your Windows 11 PC. Run `--dry-run`, then a short test, before a full video.
* **Real Piper was not available for testing.** The Piper path was tested with a stand-in program that behaves like `piper -m MODEL -f OUT.wav` with the text on stdin. The first run with your real Piper is the real test.
* Music was tested with synthetic tones only, not real songs. The loop crossfade is equal-power, which is correct for normal music; with identical pure tones it shows a small bump at the loop point.
* When music is on, the audio is mixed losslessly in between and compressed to AAC once at the end.
* Mono recordings are copied to both stereo channels. Blank paragraphs and non-text lines are ignored.
* A video longer than `batch_size` clips is encoded twice (batches, then final). That is the price of keeping RAM low. Set `join.batch_size` to a big number for a single pass if you have RAM to spare (the limit is the Windows command-line length).
* The video ends with a hard stop (no fade-out) for now.
* **Real faster-whisper was not tested.** It could not be installed in the test environment. The speech-recognition step was tested with a stand-in module that has the same interface (words with start/end times), so the timing, alignment, text file, caching and error messages are tested, but not the quality of real recognition on your voice or its speed on your CPU.
* **The FFmpeg filter check was tested with FFmpeg 6.1 and with a stand-in that prints the filter list in FFmpeg 9 format (copied from a real FFmpeg 9.0.2 run)**, not with FFmpeg 9 itself.
* **Caption rendering was tested on Linux** (FFmpeg 6.1, libass) with the Inter and DejaVu fonts, not with Windows fonts. The default fonts (Segoe UI, Georgia, Segoe UI Black) should exist on Windows 11, but run `--caption-preview` first to see the real result on your PC.
* **On-screen text was tested on Linux** (FFmpeg 6.1, libass, Inter/DejaVu fonts): every animation renders, boxes and glow look right in the test frames, and it was combined with captions in a full run. Not tested with Windows fonts or on FFmpeg 9; use `--text-preview` first.
* On-screen text is burned in like captions, so changing it means re-rendering the clips.
* Captions are burned into the video, so changing them means re-rendering the clips (use `--caption-preview` to tune the style first).
* The caption look follows your reference video's fonts, glow and animation. Its stacked, per-word layouts in different sizes at the center of the frame are not reproduced; captions are one line at a time at the bottom (your choice), with `reveal` for word-by-word appearance.
