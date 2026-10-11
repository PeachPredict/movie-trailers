"""Lightweight tests for the transcripts pipeline.

We don't exercise faster-whisper or yt-dlp here — those are integration tests.
This covers the row-shaping and decision logic, which is the unique part.
"""
from datetime import UTC, datetime
from unittest.mock import patch

from movie_trailers.pipeline.transcripts import (
    _assess_quality,
    _is_transient,
    _row,
    _Segment,
    _try_yta,
    clean_caption_text,
    keep_segment,
)

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
        text, kind, lang, err = _try_yta("v123")
    assert text is None
    assert kind is None
    assert lang is None
    assert err == "TranscriptsDisabled"


# --- quality gate + retry policy ------------------------------------------------------


def test_quality_flags_whisper_boilerplate_as_hallucinated():
    assert _assess_quality("Thanks for watching!") == "hallucinated"
    assert _assess_quality("Thanks for watching, see you in the next video!") == "hallucinated"
    assert _assess_quality(
        "What do you think of this video? Let me know in the comments below. "
        "And don't forget to like and subscribe. Thanks for watching."
    ) == "hallucinated"


def test_quality_flags_repetition_and_nn_tag():
    osa = " ".join(["OSA Digital. Entertaining the Nation."] * 8)
    assert _assess_quality(osa) == "hallucinated"
    assert _assess_quality("I'm going to make a fire. The car is fast.", language="nn") == "hallucinated"
    # Real Nynorsk text is not ASCII-only, so it passes this rule.
    assert _assess_quality("Eg veit ikkje kva som skjer no, men me må gå vidare saman", language="nn") == "ok"


def test_quality_words_per_second_sanity():
    assert _assess_quality("A few days later", duration_s=150) == "hallucinated"  # 4 words / 150 s
    fast = " ".join(f"w{i}" for i in range(400))
    assert _assess_quality(fast, duration_s=30) == "hallucinated"  # > 6 words / s
    # CJK text is not space-delimited; the rule is skipped.
    assert _assess_quality("公開 予告編 二〇二六年 全国 ロードショー 決定 新たな 物語 が 始まる 今", duration_s=150) == "ok"


def test_quality_suspect_and_ok():
    dialogue = ("I never asked for this. None of us did. But here we are, and the only way "
                "out is through. So pick up the gun and follow me.")
    assert _assess_quality(dialogue, duration_s=120, avg_logprob=-0.3, no_speech_prob=0.1) == "ok"
    assert _assess_quality(dialogue, no_speech_prob=0.7) == "suspect"
    assert _assess_quality(dialogue, avg_logprob=-1.4) == "suspect"
    assert _assess_quality("Coming soon to theaters.") == "suspect"  # < 10 words
    assert _assess_quality("") == "empty"
    assert _assess_quality(None) == "empty"


def test_row_fills_quality_and_attempts():
    row = _row("abc", source="whisper", track_kind="music", language="en",
               text="Thanks for watching!", captured_at=NOW, attempts=2)
    assert row.quality == "hallucinated"
    assert row.attempts == 2
    failed = _row("abc", source="failed", track_kind=None, language=None, text=None,
                  captured_at=NOW, error="yta=IpBlocked; whisper=yt-dlp_exit_1")
    assert failed.quality == "empty"


def test_keep_segment_drops_silence_and_boilerplate():
    assert not keep_segment(_Segment("blah", avg_logprob=-0.5, no_speech_prob=0.1, compression_ratio=3.0))
    assert not keep_segment(_Segment("blah", avg_logprob=-1.5, no_speech_prob=0.9, compression_ratio=1.0))
    assert keep_segment(_Segment("blah", avg_logprob=-1.5, no_speech_prob=0.1, compression_ratio=1.0))
    assert not keep_segment(_Segment("Thanks for watching!", -0.2, 0.1, 1.0))
    assert keep_segment(_Segment("We have to go back. Tonight. Before they find out.", -0.2, 0.1, 1.0))


def test_transient_vs_permanent_failures():
    assert _is_transient("IpBlocked", "yt-dlp_exit_1: ERROR: Sign in to confirm")
    assert _is_transient("TranscriptsDisabled", "yt-dlp_timeout")
    assert _is_transient("NoTranscriptFound", "whisper_RuntimeError")
    assert _is_transient("IpBlocked", "whisper_empty") is False
    assert _is_transient("TranscriptsDisabled", "whisper_empty") is False
    assert _is_transient("VideoUnplayable", "yt-dlp_exit_1")  # the download may work tomorrow


def test_clean_caption_text_strips_noise_tokens():
    assert clean_caption_text("[Music] >> I'm not taking a seat. [Applause] ♪ la la ♪") == (
        "I'm not taking a seat. la la"
    )
