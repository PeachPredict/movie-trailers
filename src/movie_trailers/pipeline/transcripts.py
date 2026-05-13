"""Per-trailer transcript capture.

Strategy:
1. Try `youtube-transcript-api` (scrapes YT's timedtext endpoint). Free, ~1s/trailer.
2. On failure: download audio with yt-dlp to a tempfile, transcribe with faster-whisper
   `medium`, delete the audio. No persistent cache.

Captured exactly once per trailer (guarded by `trailers.transcript_captured_at`).
A permanent failure also stamps the guard so we don't retry forever — re-fetch by
manually clearing the column.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import structlog

from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.config import Settings
from movie_trailers.models import TrailerTranscriptRow

log = structlog.get_logger()

YTA_PREFERRED_LANGS = ["en"]


def run_transcripts(
    *,
    bq: BigQueryClient,
    settings: Settings,
    today: date | None = None,
    limit: int | None = None,
) -> tuple[int, int, int]:
    """Capture transcripts for trailers that don't have one yet.

    Returns (yta_ok, whisper_ok, failures).
    """
    today = today or datetime.now(UTC).date()
    cap = limit if limit is not None else settings.transcripts_max_per_run
    target_ids = _select_targets(bq, cap)
    if not target_ids:
        log.info("transcripts.no_targets")
        return 0, 0, 0

    log.info("transcripts.start", targets=len(target_ids), cap=cap)

    rows: list[TrailerTranscriptRow] = []
    stamps: list[dict[str, Any]] = []
    yta_ok = whisper_ok = failures = 0
    whisper_holder: dict[str, Any] = {}  # lazy-load model only if needed

    for vid in target_ids:
        now = datetime.now(UTC)
        text, kind, yta_err = _try_yta(vid)
        if text:
            rows.append(_row(vid, source="yta", track_kind=kind, language="en",
                             text=text, captured_at=now))
            yta_ok += 1
            stamps.append({"youtube_video_id": vid, "captured_at": now})
            continue

        model = _load_whisper(whisper_holder, settings.whisper_model_name)
        text, lang, w_err = _try_whisper(vid, model)
        if text:
            rows.append(_row(vid, source="whisper", track_kind=None, language=lang,
                             text=text, captured_at=now))
            whisper_ok += 1
        else:
            rows.append(_row(
                vid, source="failed", track_kind=None, language=None,
                text=None, captured_at=now,
                error=f"yta={yta_err}; whisper={w_err}",
            ))
            failures += 1
        stamps.append({"youtube_video_id": vid, "captured_at": now})

    _insert_transcripts(bq, rows)
    _stamp_captured(bq, stamps)
    log.info(
        "transcripts.done",
        yta_ok=yta_ok, whisper_ok=whisper_ok, failures=failures, total=len(target_ids),
    )
    return yta_ok, whisper_ok, failures


# --- target selection ----------------------------------------------------------------

def _select_targets(bq: BigQueryClient, cap: int) -> list[str]:
    sql = f"""
    SELECT youtube_video_id
    FROM `{bq.project}.{bq.dataset}.trailers`
    WHERE transcript_captured_at IS NULL
      AND tracking_status = 'active'
    ORDER BY first_seen_at DESC
    LIMIT @cap
    """
    rows = bq.query(sql, {"cap": cap})
    return [r["youtube_video_id"] for r in rows]


# --- yta -----------------------------------------------------------------------------

def _try_yta(video_id: str) -> tuple[str | None, str | None, str | None]:
    """Returns (text, track_kind, error). text=None on failure."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import (
            NoTranscriptFound,
            TranscriptsDisabled,
            VideoUnavailable,
        )
    except ImportError as exc:
        return None, None, f"import_failed: {exc}"

    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
        track_kind: str
        try:
            t = transcript_list.find_manually_created_transcript(YTA_PREFERRED_LANGS)
            track_kind = "manual"
        except NoTranscriptFound:
            t = transcript_list.find_generated_transcript(YTA_PREFERRED_LANGS)
            track_kind = "auto-generated"
        fetched = t.fetch()
        snippets = getattr(fetched, "snippets", None) or list(fetched)
        parts = []
        for s in snippets:
            text_attr = getattr(s, "text", None)
            if text_attr is None and isinstance(s, dict):
                text_attr = s.get("text", "")
            if text_attr:
                parts.append(text_attr.strip())
        text = " ".join(" ".join(p.split()) for p in parts).strip()
        if not text:
            return None, None, "empty_transcript"
        return text, track_kind, None
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable) as exc:
        return None, None, type(exc).__name__
    except Exception as exc:  # noqa: BLE001
        return None, None, f"{type(exc).__name__}: {exc}"


# --- whisper -------------------------------------------------------------------------

def _load_whisper(holder: dict[str, Any], model_name: str):
    if "model" in holder:
        return holder["model"]
    log.info("transcripts.loading_whisper", model=model_name)
    from faster_whisper import WhisperModel
    download_root = os.environ.get("WHISPER_MODEL_DIR")  # set to /app/models in the container
    holder["model"] = WhisperModel(model_name, device="auto", download_root=download_root)
    return holder["model"]


def _try_whisper(video_id: str, model) -> tuple[str | None, str | None, str | None]:
    """Download audio to tempdir, transcribe, delete. Returns (text, language, error)."""
    if shutil.which("yt-dlp") is None:
        return None, None, "yt-dlp_not_installed"

    with tempfile.TemporaryDirectory(prefix="mt-whisper-") as tmp:
        tmpdir = Path(tmp)
        try:
            subprocess.run(
                [
                    "yt-dlp",
                    "-f", "bestaudio/best",
                    "-x", "--audio-format", "m4a",
                    "-o", str(tmpdir / "%(id)s.%(ext)s"),
                    "--no-warnings", "--quiet",
                    f"https://www.youtube.com/watch?v={video_id}",
                ],
                check=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return None, None, "yt-dlp_timeout"
        except subprocess.CalledProcessError as exc:
            return None, None, f"yt-dlp_exit_{exc.returncode}"
        except Exception as exc:  # noqa: BLE001
            return None, None, f"yt-dlp_{type(exc).__name__}"

        audio_files = list(tmpdir.iterdir())
        if not audio_files:
            return None, None, "yt-dlp_no_output"
        audio_path = audio_files[0]

        try:
            segments, info = model.transcribe(str(audio_path), vad_filter=True)
            text = " ".join(seg.text.strip() for seg in segments).strip()
            if not text:
                return None, None, "whisper_empty"
            return text, info.language, None
        except Exception as exc:  # noqa: BLE001
            return None, None, f"whisper_{type(exc).__name__}: {exc}"


# --- writers -------------------------------------------------------------------------

def _row(
    video_id: str,
    *,
    source: str,
    track_kind: str | None,
    language: str | None,
    text: str | None,
    captured_at: datetime,
    error: str | None = None,
) -> TrailerTranscriptRow:
    wc = len(text.split()) if text else None
    cc = len(text) if text else None
    return TrailerTranscriptRow(
        youtube_video_id=video_id,
        source=source,  # type: ignore[arg-type]
        track_kind=track_kind,
        language=language,
        text=text,
        word_count=wc,
        char_count=cc,
        error=error,
        captured_at=captured_at,
    )


def _insert_transcripts(bq: BigQueryClient, rows: list[TrailerTranscriptRow]) -> None:
    if not rows:
        return
    fields = [
        "youtube_video_id", "source", "track_kind", "language", "text",
        "word_count", "char_count", "error", "captured_at",
    ]
    bq.merge_rows(
        table="trailer_transcripts",
        rows=rows,
        merge_keys=["youtube_video_id"],
        update_fields=[c for c in fields if c != "youtube_video_id"],
        insert_fields=fields,
    )


def _stamp_captured(bq: BigQueryClient, stamps: list[dict[str, Any]]) -> None:
    if not stamps:
        return
    bq.update_from_dicts(
        table="trailers",
        rows=stamps,
        merge_keys=["youtube_video_id"],
        update_clause_sql="T.transcript_captured_at = S.captured_at",
    )
