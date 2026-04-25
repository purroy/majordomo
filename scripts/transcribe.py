#!/usr/bin/env python3
"""Transcribe an audio file with whisper.cpp.

Reads paths from env (with sensible Linux defaults):
  WHISPER_BIN    path to whisper-cli binary
  WHISPER_MODEL  path to .bin model (ggml format)

Usage:  python3 scripts/transcribe.py <audio-file> [language|auto]

Whisper.cpp natively reads flac/mp3/ogg/wav. For other formats (e.g.
.oga voice notes that some clients send, or .mp4 video_notes) we
transparently transcode to wav 16kHz mono via ffmpeg first.

Prints the transcribed text to stdout. Exits non-zero on failure.
Stdlib only.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

WHISPER_BIN = os.environ.get(
    "WHISPER_BIN", "/opt/whisper.cpp/build/bin/whisper-cli"
)
WHISPER_MODEL = os.environ.get(
    "WHISPER_MODEL", "/opt/whisper.cpp/models/ggml-medium.bin"
)

DIRECT_EXTS = {".wav", ".mp3", ".ogg", ".flac"}


def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kw)


def to_wav(src: Path, dst: Path) -> None:
    """Transcode any input to wav 16kHz mono using ffmpeg."""
    ff = run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(src), "-ac", "1", "-ar", "16000", str(dst),
    ])
    if ff.returncode != 0:
        raise RuntimeError(f"ffmpeg rc={ff.returncode}: {ff.stderr.strip()[:300]}")


def transcribe(src: Path, lang: str = "auto") -> str:
    if not Path(WHISPER_BIN).exists():
        raise RuntimeError(f"whisper-cli missing at {WHISPER_BIN}")
    if not Path(WHISPER_MODEL).exists():
        raise RuntimeError(f"model missing at {WHISPER_MODEL}")

    with tempfile.TemporaryDirectory(prefix="pa-trans-") as td:
        td_p = Path(td)
        # whisper.cpp accepts ogg/mp3/wav/flac directly; transcode others.
        if src.suffix.lower() in DIRECT_EXTS:
            input_for_whisper = src
        else:
            input_for_whisper = td_p / "in.wav"
            to_wav(src, input_for_whisper)

        out_prefix = td_p / "out"
        cmd = [
            WHISPER_BIN, "-m", WHISPER_MODEL, "-f", str(input_for_whisper),
            "-nt", "-np",                  # no timestamps, no progress
            "-otxt", "-of", str(out_prefix),
        ]
        if lang and lang != "auto":
            cmd += ["-l", lang]
        # Add a generous timeout — medium on CPU runs ~real-time.
        w = run(cmd, timeout=600)
        if w.returncode != 0:
            raise RuntimeError(
                f"whisper rc={w.returncode}: {w.stderr.strip()[:300]}"
            )
        out_txt = out_prefix.with_suffix(".txt")
        if not out_txt.exists():
            raise RuntimeError("whisper produced no .txt output")
        return out_txt.read_text(encoding="utf-8").strip()


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: transcribe.py <audio-file> [language|auto]", file=sys.stderr)
        return 2
    src = Path(sys.argv[1])
    if not src.exists():
        print(f"input not found: {src}", file=sys.stderr)
        return 1
    lang = sys.argv[2] if len(sys.argv) > 2 else "auto"
    try:
        text = transcribe(src, lang)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(str(e), file=sys.stderr)
        return 3
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
