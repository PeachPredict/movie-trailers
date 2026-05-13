"""Lightweight tests for the transcripts pipeline.

We don't exercise faster-whisper or yt-dlp here — those are integration tests.
This covers the row-shaping and decision logic, which is the unique part.
"""
from datetime import UTC, datetime
from unittest.mock import patch

from movie_trailers.pipeline.transcripts import _row, _try_yta

NOW = datetime(2026, 5, 12, 21, 30, tzinfo=UTC)


def test_row_for_successful_yta_sets_counts():
    row = _row(
        "abc",
        source="yta",
        track_kind="manual",
        language="en",
        text="hello world hello",
        captured_at=NOW,
    )
    assert row.source == "yta"
    assert row.track_kind == "manual"
    assert row.language == "en"
    assert row.text == "hello world hello"
    assert row.word_count == 3
    assert row.char_count == 17
    assert row.error is None


def test_row_for_failure_leaves_counts_none():
    row = _row(
        "abc",
        source="failed",
        track_kind=None,
        language=None,
        text=None,
        captured_at=NOW,
        error="yta=TranscriptsDisabled; whisper=yt-dlp_exit_1",
    )
    assert row.source == "failed"
    assert row.text is None
    assert row.word_count is None
    assert row.char_count is None
    assert row.error.startswith("yta=")


def test_try_yta_returns_error_on_transcripts_disabled():
    """The function should classify the error, not crash."""
    from youtube_transcript_api._errors import TranscriptsDisabled

    fake_exc = TranscriptsDisabled("v123")

    class FakeAPI:
        def list(self, _vid):
            raise fake_exc

    with patch("youtube_transcript_api.YouTubeTranscriptApi", return_value=FakeAPI()):
        text, kind, err = _try_yta("v123")
    assert text is None
    assert kind is None
    assert err == "TranscriptsDisabled"
