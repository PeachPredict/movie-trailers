"""Per-trailer transcript capture.

Strategy:
1. Try `youtube-transcript-api` in the title's languages (manual track first, then
   auto-generated). Free, ~1s/trailer.
2. On failure: download audio with yt-dlp to a tempfile, transcribe with faster-whisper
   (VAD-on pass for speech, VAD-off pass to rescue song-driven trailers), delete the audio.

Every attempt writes one `trailer_transcripts` row (MERGE on youtube_video_id) carrying
the confidence signals Whisper reports plus a `quality` verdict from `_assess_quality`
(`ok` | `suspect` | `hallucinated` | `empty`). Consumers should filter on `quality='ok'`.

`trailers.transcript_captured_at` is stamped on success and on *permanent* failure
(captions disabled + nothing to transcribe, unplayable, age-restricted). *Transient*
failures (IP blocks, yt-dlp errors, Whisper crashes) leave it NULL and bump `attempts`,
so the trailer is retried on later runs up to MAX_ATTEMPTS. `mt requeue-transcripts`
clears the stamp for rows that deserve another pass (music-pass hallucinations etc.).
"""
from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import structlog

from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.config import Settings
from movie_trailers.models import TrailerTranscriptRow
from movie_trailers.pipeline._common import title_languages

log = structlog.get_logger()

MAX_ATTEMPTS = 3  # transient failures are retried on later runs up to this many times

# --- Whisper segment filtering (faster-whisper's own decision thresholds) ---
SEG_MAX_COMPRESSION_RATIO = 2.4  # above: degenerate repetition
SEG_NO_SPEECH_PROB = 0.6  # with a low logprob: Whisper itself calls the segment silent
SEG_MIN_AVG_LOGPROB = -1.0
LANG_HINT_MIN_PROB = 0.6  # detected language below this confidence + off-title → hint & re-run

# Phrases Whisper emits over music/silence (training-data artefacts), lower-cased,
# accent-stripped. Matched as substrings of the normalised text.
HALLUCINATION_PHRASES = (
    "thanks for watching", "thank you for watching", "thanks for listening",
    "subtitles by", "subtitle by", "amara.org", "please subscribe", "like and subscribe",
    "subscribe to", "hit that like button", "see you in the next video",
    "let me know in the comments", "don't forget to like", "dont forget to like",
    "sous-titres", "sous-titrage", "untertitel", "legendas por", "legendado por",
    "ご視聴ありがとうございました", "チャンネル登録", "字幕由", "謝謝觀看", "谢谢观看",
    "시청해주셔서 감사합니다", "구독",
)
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
_CAPTION_NOISE_RE = re.compile(r"\[[^\]]{1,30}\]|\(\s*(?:music|applause|laughter)\s*\)|>>|♪+|🎵", re.I)

TRANSIENT_YTA = ("IpBlocked", "RequestBlocked", "TooManyRequests", "import_failed")
TRANSIENT_WHISPER_PREFIXES = ("yt-dlp_", "import_failed")


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
    targets = _select_targets(bq, cap, max_attempts=MAX_ATTEMPTS)
    if not targets:
        log.info("transcripts.no_targets")
        return 0, 0, 0

    log.info("transcripts.start", targets=len(targets), cap=cap)

    rows: list[TrailerTranscriptRow] = []
    stamps: list[dict[str, Any]] = []
    yta_ok = whisper_ok = failures = 0
    whisper_holder: dict[str, Any] = {}  # lazy-load model only if needed
    model_tag = f"whisper-{settings.whisper_model_name}"

    for t in targets:
        vid = t["youtube_video_id"]
        attempts = int(t.get("attempts") or 0) + 1
        duration = t.get("duration_seconds")
        languages = _languages_for(t)
        now = datetime.now(UTC)

        text, kind, lang, yta_err = _try_yta(vid, languages)
        if text:
            rows.append(_row(vid, source="yta", track_kind=kind, language=lang, text=text,
                             captured_at=now, model="yta", audio_duration_s=duration,
                             attempts=attempts))
            yta_ok += 1
            stamps.append({"youtube_video_id": vid, "captured_at": now})
            continue

        model = _load_whisper(whisper_holder, settings.whisper_model_name)
        w = _try_whisper(vid, model, expected_languages=languages)
        if w.text:
            rows.append(_row(
                vid, source="whisper", track_kind=w.track_kind, language=w.language,
                text=w.text, captured_at=now, model=model_tag,
                language_probability=w.language_probability, avg_logprob=w.avg_logprob,
                no_speech_prob=w.no_speech_prob, compression_ratio=w.compression_ratio,
                audio_duration_s=w.duration or duration, attempts=attempts,
            ))
            whisper_ok += 1
            stamps.append({"youtube_video_id": vid, "captured_at": now})
            continue

        err = f"yta={_short_err(yta_err)}; whisper={_short_err(w.error)}"
        if w.error_detail:
            err += f" | {w.error_detail}"
        rows.append(_row(vid, source="failed", track_kind=None, language=None, text=None,
                         captured_at=now, error=err, model=model_tag,
                         audio_duration_s=duration, attempts=attempts))
        failures += 1
        transient = _is_transient(yta_err, w.error)
        if not transient or attempts >= MAX_ATTEMPTS:
            stamps.append({"youtube_video_id": vid, "captured_at": now})
        else:
            log.info("transcripts.will_retry", video_id=vid, attempts=attempts, error=err)

    _insert_transcripts(bq, rows)
    _stamp_captured(bq, stamps)
    log.info(
        "transcripts.done",
        yta_ok=yta_ok, whisper_ok=whisper_ok, failures=failures, total=len(targets),
        quality={q: sum(1 for r in rows if r.quality == q) for q in
                 ("ok", "suspect", "hallucinated", "empty")},
    )
    return yta_ok, whisper_ok, failures


# --- target selection ----------------------------------------------------------------

def _select_targets(bq: BigQueryClient, cap: int, *, max_attempts: int) -> list[dict[str, Any]]:
    """Trailers lacking a transcript stamp, active first then ended, newest first.

    `trailers` is polymorphic — a youtube_video_id can have a movie row AND a tv-season
    row — so rows are collapsed per video. The title's language metadata rides along
    (yta track choice + Whisper language hint); `attempts` excludes videos that already
    failed transiently `max_attempts` times.
    """
    sql = f"""
    WITH t AS (
      SELECT youtube_video_id,
             MAX(first_seen_at) AS first_seen_at,
             MIN(IF(tracking_status = 'active', 0, 1)) AS status_rank,
             ANY_VALUE(movie_tmdb_id) AS movie_tmdb_id,
             ANY_VALUE(tv_tmdb_id) AS tv_tmdb_id,
             ANY_VALUE(duration_seconds) AS duration_seconds
      FROM `{bq.project}.{bq.dataset}.trailers`
      WHERE transcript_captured_at IS NULL
        AND tracking_status IN ('active', 'ended')
      GROUP BY youtube_video_id
    )
    SELECT t.youtube_video_id, t.duration_seconds,
           COALESCE(m.original_language, s.original_language) AS original_language,
           COALESCE(m.spoken_languages, s.spoken_languages) AS spoken_languages,
           COALESCE(x.attempts, 0) AS attempts
    FROM t
    LEFT JOIN `{bq.project}.{bq.dataset}.movies` m ON m.tmdb_id = t.movie_tmdb_id
    LEFT JOIN `{bq.project}.{bq.dataset}.tv_shows` s ON s.tmdb_id = t.tv_tmdb_id
    LEFT JOIN `{bq.project}.{bq.dataset}.trailer_transcripts` x USING (youtube_video_id)
    WHERE COALESCE(x.attempts, 0) < @max_attempts
    ORDER BY t.status_rank, t.first_seen_at DESC
    LIMIT @cap
    """
    return bq.query(sql, {"cap": cap, "max_attempts": max_attempts})


def _languages_for(target: dict[str, Any]) -> list[str]:
    """Title languages (original + spoken for non-English titles) with English last."""
    langs = title_languages(target) if target.get("original_language") else []
    if "en" not in langs:
        langs.append("en")
    return langs


# --- error classification ------------------------------------------------------------

def _short_err(err: str | None) -> str:
    """Collapse a verbose yta/whisper error string to its leading tag.

    yta's `IpBlocked` / `VideoUnplayable` exceptions stringify to ~1.5 KB of boilerplate.
    Strings already produced as short tags (e.g. ``"TranscriptsDisabled"``,
    ``"yt-dlp_exit_1"``) pass through unchanged.
    """
    if not err:
        return str(err)
    head, _, _ = err.partition(":")
    return head.strip() or err


def _is_transient(yta_err: str | None, whisper_err: str | None) -> bool:
    """A failure is worth retrying when the *fallback* (Whisper) failed for a reason
    that can clear by itself: a blocked IP, a yt-dlp extractor/network error, a crash.
    `whisper_empty` (no speech at all) and yta's permanent verdicts are final."""
    w = _short_err(whisper_err)
    if w == "whisper_empty":
        return False
    if w.startswith(TRANSIENT_WHISPER_PREFIXES) or re.match(r"whisper_[A-Z]\w*", w):
        return True
    return _short_err(yta_err) in TRANSIENT_YTA


# --- yta -----------------------------------------------------------------------------

_YTA_JITTER_SECONDS = (0.5, 1.5)


def clean_caption_text(text: str) -> str:
    """Drop caption noise tokens ([Music], [Applause], '>>', ♪) and collapse whitespace."""
    return " ".join(_CAPTION_NOISE_RE.sub(" ", text).split())


def _try_yta(
    video_id: str, languages: list[str] | None = None
) -> tuple[str | None, str | None, str | None, str | None]:
    """Returns (text, track_kind, language, error). text=None on failure."""
    languages = languages or ["en"]
    # Jittered delay before every yta call. YouTube publishes no rate limit; this
    # spaces out bursts so a sequence of yta-success videos doesn't trip bot
    # detection. Adds ~4-12 min to a 500-video run.
    time.sleep(random.uniform(*_YTA_JITTER_SECONDS))
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import (
            NoTranscriptFound,
            TranscriptsDisabled,
            VideoUnavailable,
        )
    except ImportError as exc:
        return None, None, None, f"import_failed: {exc}"

    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
        track_kind: str
        try:
            t = transcript_list.find_manually_created_transcript(languages)
            track_kind = "manual"
        except NoTranscriptFound:
            t = transcript_list.find_generated_transcript(languages)
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
        text = clean_caption_text(" ".join(parts))
        if not text:
            return None, None, None, "empty_transcript"
        lang = getattr(t, "language_code", None) or languages[0]
        return text, track_kind, str(lang).split("-")[0], None
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable) as exc:
        return None, None, None, type(exc).__name__
    except Exception as exc:  # noqa: BLE001
        return None, None, None, f"{type(exc).__name__}: {exc}"


# --- whisper -------------------------------------------------------------------------

def _load_whisper(holder: dict[str, Any], model_name: str):
    if "model" in holder:
        return holder["model"]
    log.info("transcripts.loading_whisper", model=model_name)
    from faster_whisper import WhisperModel
    download_root = os.environ.get("WHISPER_MODEL_DIR")  # set to /app/models in the container
    holder["model"] = WhisperModel(model_name, device="auto", download_root=download_root)
    return holder["model"]


@dataclass
class WhisperResult:
    text: str | None = None
    language: str | None = None
    language_probability: float | None = None
    track_kind: str | None = None  # 'speech' (VAD-on pass) | 'music' (VAD-off rescue)
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None
    duration: float | None = None
    error: str | None = None
    error_detail: str | None = None


@dataclass
class _Segment:
    text: str
    avg_logprob: float | None
    no_speech_prob: float | None
    compression_ratio: float | None


def keep_segment(seg: _Segment) -> bool:
    """Whisper's own silence/degeneration rules + the hallucination phrase list."""
    if seg.compression_ratio is not None and seg.compression_ratio > SEG_MAX_COMPRESSION_RATIO:
        return False
    if (
        seg.no_speech_prob is not None and seg.no_speech_prob > SEG_NO_SPEECH_PROB
        and seg.avg_logprob is not None and seg.avg_logprob < SEG_MIN_AVG_LOGPROB
    ):
        return False
    norm = _norm(seg.text)
    return not (len(norm.split()) < 8 and any(p in norm for p in HALLUCINATION_PHRASES))


def _run_pass(model, audio_path: str, *, vad: bool, language: str | None):
    segments, info = model.transcribe(
        audio_path,
        vad_filter=vad,
        language=language,
        condition_on_previous_text=False,  # stops "X. X. X. X." runaway repetition
    )
    segs = [
        _Segment(
            text=(s.text or "").strip(),
            avg_logprob=getattr(s, "avg_logprob", None),
            no_speech_prob=getattr(s, "no_speech_prob", None),
            compression_ratio=getattr(s, "compression_ratio", None),
        )
        for s in segments  # generator: consuming it runs the decode
    ]
    return [s for s in segs if s.text and keep_segment(s)], info


def _mean(vals: list[float | None]) -> float | None:
    xs = [v for v in vals if v is not None]
    return (sum(xs) / len(xs)) if xs else None


def _try_whisper(
    video_id: str, model, *, expected_languages: list[str] | None = None
) -> WhisperResult:
    """Download audio to tempdir, transcribe, delete.

    Pass 1 is VAD-filtered ("speech"). If Whisper's detected language is neither one of
    the title's languages nor English and its confidence is low, the pass is re-run with
    the title's original language as a hint (trailers open with logo music, which fools
    detection). Pass 2 (VAD off, "music") rescues song-driven trailers — Bollywood,
    Tamil/Telugu, K-pop teasers — where VAD discards singing as non-speech.
    """
    if shutil.which("yt-dlp") is None:
        return WhisperResult(error="yt-dlp_not_installed")

    expected = [lang for lang in (expected_languages or []) if lang]
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
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            return WhisperResult(error="yt-dlp_timeout")
        except subprocess.CalledProcessError as exc:
            return WhisperResult(
                error=f"yt-dlp_exit_{exc.returncode}", error_detail=_tail(exc.stderr)
            )
        except Exception as exc:  # noqa: BLE001
            return WhisperResult(error=f"yt-dlp_{type(exc).__name__}", error_detail=_tail(str(exc)))

        audio_files = list(tmpdir.iterdir())
        if not audio_files:
            return WhisperResult(error="yt-dlp_no_output")
        audio_path = str(audio_files[0])

        try:
            hint: str | None = None
            segs, info = _run_pass(model, audio_path, vad=True, language=None)
            detected = getattr(info, "language", None)
            prob = getattr(info, "language_probability", None) or 0.0
            if (
                expected and detected not in {*expected, "en"} and prob < LANG_HINT_MIN_PROB
            ):
                hint = expected[0]
                log.info("transcripts.language_hint", video_id=video_id,
                         detected=detected, prob=round(prob, 2), hint=hint)
                segs, info = _run_pass(model, audio_path, vad=True, language=hint)
            kind = "speech"
            if not segs:
                segs, info = _run_pass(model, audio_path, vad=False, language=hint)
                kind = "music"
            if not segs:
                return WhisperResult(error="whisper_empty",
                                     duration=getattr(info, "duration", None))
            return WhisperResult(
                text=" ".join(s.text for s in segs),
                language=getattr(info, "language", None),
                language_probability=getattr(info, "language_probability", None),
                track_kind=kind,
                avg_logprob=_mean([s.avg_logprob for s in segs]),
                no_speech_prob=_mean([s.no_speech_prob for s in segs]),
                compression_ratio=_mean([s.compression_ratio for s in segs]),
                duration=getattr(info, "duration", None),
            )
        except Exception as exc:  # noqa: BLE001
            return WhisperResult(error=f"whisper_{type(exc).__name__}", error_detail=_tail(str(exc)))


def _tail(s: str | None, n: int = 200) -> str | None:
    if not s:
        return None
    return " ".join(s.split())[-n:]


# --- quality gate --------------------------------------------------------------------

def _norm(text: str) -> str:
    s = unicodedata.normalize("NFKD", text).casefold()
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")


def _assess_quality(
    text: str | None,
    *,
    language: str | None = None,
    duration_s: float | None = None,
    avg_logprob: float | None = None,
    no_speech_prob: float | None = None,
    compression_ratio: float | None = None,
) -> str:
    """'ok' | 'suspect' | 'hallucinated' | 'empty' from text heuristics + Whisper stats.

    Whisper hallucinates over music and silence: training-data boilerplate ("Thanks for
    watching!"), runaway repetition, and a `nn` (Norwegian Nynorsk) language tag on
    English boilerplate are the signatures seen in this dataset.
    """
    if not text or not text.strip():
        return "empty"
    norm = _norm(text)
    words = norm.split()
    n = len(words)
    cjk = bool(_CJK_RE.search(text))
    phrase_hits = [p for p in HALLUCINATION_PHRASES if p in norm]
    other_words = n - sum(len(p.split()) for p in phrase_hits)
    distinct_ratio = (len(set(words)) / n) if n else 0.0
    wps = (n / duration_s) if duration_s and duration_s > 0 and not cjk else None

    if phrase_hits and other_words < 15:
        return "hallucinated"
    if n >= 20 and distinct_ratio < 0.3:
        return "hallucinated"
    if language == "nn" and text.isascii():
        return "hallucinated"
    if wps is not None and (wps > 6 or wps < 0.05):
        return "hallucinated"

    if n < 10 or phrase_hits:
        return "suspect"
    if no_speech_prob is not None and no_speech_prob > 0.5:
        return "suspect"
    if avg_logprob is not None and avg_logprob < SEG_MIN_AVG_LOGPROB:
        return "suspect"
    if compression_ratio is not None and compression_ratio > 2.0:
        return "suspect"
    return "ok"


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
    model: str | None = None,
    language_probability: float | None = None,
    avg_logprob: float | None = None,
    no_speech_prob: float | None = None,
    compression_ratio: float | None = None,
    audio_duration_s: float | None = None,
    attempts: int = 1,
    quality: str | None = None,
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
        model=model,
        language_probability=language_probability,
        avg_logprob=avg_logprob,
        no_speech_prob=no_speech_prob,
        compression_ratio=compression_ratio,
        audio_duration_s=audio_duration_s,
        quality=quality or _assess_quality(
            text, language=language, duration_s=audio_duration_s, avg_logprob=avg_logprob,
            no_speech_prob=no_speech_prob, compression_ratio=compression_ratio,
        ),
        attempts=attempts,
    )


_FIELDS = list(TrailerTranscriptRow.model_fields.keys())


def _insert_transcripts(bq: BigQueryClient, rows: list[TrailerTranscriptRow]) -> None:
    if not rows:
        return
    bq.merge_rows(
        table="trailer_transcripts",
        rows=rows,
        merge_keys=["youtube_video_id"],
        update_fields=[c for c in _FIELDS if c != "youtube_video_id"],
        insert_fields=_FIELDS,
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


# --- requeue (one-off repair) --------------------------------------------------------

REQUEUE_WHERE: dict[str, str] = {
    # VAD-off rescue pass: in the 2026-06 cohort ~70% of these were hallucinated.
    "music": "x.track_kind = 'music'",
    # Gate verdicts (new rows) + the pre-gate signatures (old rows have quality NULL).
    "hallucinated": (
        "x.quality IN ('hallucinated', 'suspect') OR x.language = 'nn'"
        " OR (x.text IS NOT NULL AND x.word_count < 25 AND REGEXP_CONTAINS("
        "LOWER(x.text), r'thanks for watching|thank you for watching|subtitles by|amara\\.org'))"
    ),
    "short": "x.source != 'failed' AND x.word_count < 10",
    "transient": (
        "x.source = 'failed' AND NOT REGEXP_CONTAINS(x.error, r'whisper=whisper_empty')"
        " AND REGEXP_CONTAINS(x.error, r'whisper=(yt-dlp_|whisper_[A-Z]|import_failed)"
        "|yta=(IpBlocked|RequestBlocked|TooManyRequests)')"
    ),
}


def requeue_transcripts(
    bq: BigQueryClient, *, where: list[str], dry_run: bool = False
) -> list[str]:
    """Clear `transcript_captured_at` (and reset `attempts`) for rows matching `where`
    keys of REQUEUE_WHERE, so the next transcripts run re-processes them. Returns ids."""
    conds = [f"({REQUEUE_WHERE[w]})" for w in where]
    sql = f"""
    SELECT DISTINCT x.youtube_video_id
    FROM `{bq.project}.{bq.dataset}.trailer_transcripts` x
    JOIN `{bq.project}.{bq.dataset}.trailers` t USING (youtube_video_id)
    WHERE t.tracking_status IN ('active', 'ended') AND ({' OR '.join(conds)})
    """
    ids = [r["youtube_video_id"] for r in bq.query(sql)]
    if dry_run or not ids:
        return ids
    rows = [{"youtube_video_id": v} for v in ids]
    bq.update_from_dicts(
        table="trailers", rows=rows, merge_keys=["youtube_video_id"],
        update_clause_sql="T.transcript_captured_at = NULL",
    )
    bq.update_from_dicts(
        table="trailer_transcripts", rows=rows, merge_keys=["youtube_video_id"],
        update_clause_sql="T.attempts = 0",
    )
    return ids
