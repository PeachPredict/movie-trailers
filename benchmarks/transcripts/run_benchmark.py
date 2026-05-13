"""Benchmark youtube-transcript-api vs faster-whisper on a fixed set of trailers.

Run:
    cd benchmarks/transcripts
    uv run --with youtube-transcript-api --with yt-dlp --with faster-whisper python run_benchmark.py

Outputs:
    results.json          # full per-trailer numbers
    results.md            # human-readable summary table
    out/<video_id>.yta.txt   # transcript captured via youtube-transcript-api
    out/<video_id>.whisper.txt   # transcript captured via Whisper
    audio/<video_id>.m4a  # downloaded audio (kept for inspection; safe to delete)
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

HERE = Path(__file__).parent
OUT_DIR = HERE / "out"
AUDIO_DIR = HERE / "audio"
TRAILERS_JSON = HERE / "trailers.json"
RESULTS_JSON = HERE / "results.json"
RESULTS_MD = HERE / "results.md"

# Whisper model. base = fast, ~150MB, good baseline; bump to 'medium' or
# 'large-v3' for higher accuracy at higher cost.
WHISPER_MODEL = "small"
WHISPER_DEVICE = "auto"  # 'cpu', 'cuda', or 'auto' — faster-whisper picks Metal on Apple Silicon


@dataclass
class MethodResult:
    success: bool = False
    error: str | None = None
    latency_seconds: float = 0.0
    char_count: int = 0
    word_count: int = 0
    sample: str = ""  # first 200 chars
    transcript_path: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class TrailerResult:
    youtube_video_id: str
    movie_title: str
    duration_seconds: int
    view_count: int
    yta: MethodResult = field(default_factory=MethodResult)
    whisper: MethodResult = field(default_factory=MethodResult)
    similarity_jaccard: float | None = None


def normalize(text: str) -> list[str]:
    text = text.lower()
    text = re.sub(r"[^\w\s']", " ", text)
    return [w for w in text.split() if w]


def jaccard(a: str, b: str) -> float:
    sa, sb = set(normalize(a)), set(normalize(b))
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# --- Method A: youtube-transcript-api -----------------------------------------------

def run_yta(video_id: str) -> tuple[str, MethodResult]:
    """youtube-transcript-api v1.x API: instance-based, .list() + .fetch()."""
    res = MethodResult()
    started = time.perf_counter()
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import (
            NoTranscriptFound,
            TranscriptsDisabled,
            VideoUnavailable,
        )

        api = YouTubeTranscriptApi()
        try:
            transcript_list = api.list(video_id)
            try:
                transcript_obj = transcript_list.find_manually_created_transcript(["en"])
                res.extra["track_kind"] = "manual"
            except NoTranscriptFound:
                transcript_obj = transcript_list.find_generated_transcript(["en"])
                res.extra["track_kind"] = "auto-generated"
            fetched = transcript_obj.fetch()
        except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable) as exc:
            res.error = type(exc).__name__
            res.latency_seconds = time.perf_counter() - started
            return "", res

        # fetched is a FetchedTranscript with .snippets, each having .text
        snippets = getattr(fetched, "snippets", None) or list(fetched)
        text = " ".join(
            (getattr(s, "text", None) or s.get("text", "") if isinstance(s, dict) else getattr(s, "text", "")).strip()
            for s in snippets
        )
        text = " ".join(text.split())  # collapse whitespace
        res.success = True
        res.char_count = len(text)
        res.word_count = len(normalize(text))
        res.sample = text[:200]
        res.latency_seconds = time.perf_counter() - started
        out_path = OUT_DIR / f"{video_id}.yta.txt"
        out_path.write_text(text)
        res.transcript_path = str(out_path.relative_to(HERE))
        return text, res
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
        res.latency_seconds = time.perf_counter() - started
        return "", res


# --- Method B: Whisper (faster-whisper) on downloaded audio --------------------------

def download_audio(video_id: str) -> Path | None:
    """Use yt-dlp to fetch the best audio-only stream. Returns the saved file path."""
    target_glob = list(AUDIO_DIR.glob(f"{video_id}.*"))
    if target_glob:
        return target_glob[0]
    AUDIO_DIR.mkdir(exist_ok=True)
    if shutil.which("yt-dlp") is None:
        raise RuntimeError("yt-dlp not on PATH; install via `brew install yt-dlp`")
    cmd = [
        "yt-dlp",
        "-f", "bestaudio/best",
        "-x", "--audio-format", "m4a",
        "-o", str(AUDIO_DIR / "%(id)s.%(ext)s"),
        "--no-warnings",
        "--quiet",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    subprocess.run(cmd, check=True, timeout=120)
    matches = list(AUDIO_DIR.glob(f"{video_id}.*"))
    return matches[0] if matches else None


def run_whisper(video_id: str, model_ref: dict) -> tuple[str, MethodResult]:
    res = MethodResult()
    started = time.perf_counter()
    try:
        from faster_whisper import WhisperModel

        if "model" not in model_ref:
            print(f"  [whisper] loading model={WHISPER_MODEL} ...", flush=True)
            model_ref["model"] = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE)
        model: WhisperModel = model_ref["model"]

        audio_path = download_audio(video_id)
        if audio_path is None or not audio_path.exists():
            res.error = "audio_download_failed"
            res.latency_seconds = time.perf_counter() - started
            return "", res
        res.extra["audio_bytes"] = audio_path.stat().st_size

        segments, info = model.transcribe(
            str(audio_path), language="en", vad_filter=True
        )
        text = " ".join(seg.text.strip() for seg in segments)
        res.extra["detected_language"] = info.language
        res.extra["language_probability"] = round(info.language_probability, 3)
        res.success = True
        res.char_count = len(text)
        res.word_count = len(normalize(text))
        res.sample = text[:200]
        res.latency_seconds = time.perf_counter() - started
        out_path = OUT_DIR / f"{video_id}.whisper.txt"
        out_path.write_text(text)
        res.transcript_path = str(out_path.relative_to(HERE))
        return text, res
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
        res.latency_seconds = time.perf_counter() - started
        return "", res


# --- Orchestrator -------------------------------------------------------------------

def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    AUDIO_DIR.mkdir(exist_ok=True)
    trailers = json.loads(TRAILERS_JSON.read_text())
    print(f"Loaded {len(trailers)} trailers from {TRAILERS_JSON.name}")
    print(f"Whisper model: {WHISPER_MODEL} (device={WHISPER_DEVICE})\n")

    results: list[TrailerResult] = []
    whisper_model_ref: dict = {}

    for i, t in enumerate(trailers, 1):
        vid = t["youtube_video_id"]
        print(f"[{i}/{len(trailers)}] {vid}  {t['movie_title']!r} ({t['duration_seconds']}s)")
        r = TrailerResult(
            youtube_video_id=vid,
            movie_title=t["movie_title"],
            duration_seconds=t["duration_seconds"],
            view_count=t["view_count"],
        )

        text_a, r.yta = run_yta(vid)
        status = "OK" if r.yta.success else f"FAIL ({r.yta.error})"
        print(f"  yta:     {status:35s}  {r.yta.latency_seconds:6.2f}s  {r.yta.word_count} words")

        text_b, r.whisper = run_whisper(vid, whisper_model_ref)
        status = "OK" if r.whisper.success else f"FAIL ({r.whisper.error})"
        print(f"  whisper: {status:35s}  {r.whisper.latency_seconds:6.2f}s  {r.whisper.word_count} words")

        if r.yta.success and r.whisper.success:
            r.similarity_jaccard = round(jaccard(text_a, text_b), 3)
            print(f"  jaccard: {r.similarity_jaccard}")
        print()
        results.append(r)

    RESULTS_JSON.write_text(json.dumps([asdict(r) for r in results], indent=2))
    write_markdown_report(results)
    print(f"Wrote {RESULTS_JSON.name} and {RESULTS_MD.name}")
    return 0


def write_markdown_report(results: list[TrailerResult]) -> None:
    lines: list[str] = []
    lines.append("# Transcript benchmark results\n")
    lines.append(f"Whisper model: `{WHISPER_MODEL}` on `{WHISPER_DEVICE}`.\n")
    lines.append("| video | duration | yta | yta sec | yta words | whisper | wh sec | wh words | jaccard |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        yta_status = "✓" if r.yta.success else f"✗ {r.yta.error}"
        wh_status = "✓" if r.whisper.success else f"✗ {r.whisper.error}"
        lines.append(
            f"| `{r.youtube_video_id}` ({r.movie_title}) | {r.duration_seconds}s | {yta_status} | "
            f"{r.yta.latency_seconds:.2f} | {r.yta.word_count} | {wh_status} | "
            f"{r.whisper.latency_seconds:.2f} | {r.whisper.word_count} | "
            f"{r.similarity_jaccard if r.similarity_jaccard is not None else '—'} |"
        )
    lines.append("")
    lines.append("Successes / total: "
                 f"yta {sum(1 for r in results if r.yta.success)}/{len(results)}, "
                 f"whisper {sum(1 for r in results if r.whisper.success)}/{len(results)}")
    lines.append("")
    lines.append("Inspect `out/<video_id>.{yta,whisper}.txt` for the full transcripts.")
    RESULTS_MD.write_text("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
