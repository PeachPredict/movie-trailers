from datetime import UTC, date, datetime

from movie_trailers.digest.queries import cutoff_for_period
from movie_trailers.digest.render import render_digest_html


def test_cutoff_for_period_week_and_month():
    today = date(2026, 5, 15)
    assert cutoff_for_period("week", today=today) == date(2026, 5, 8)
    assert cutoff_for_period("month", today=today) == date(2026, 4, 15)


def test_cutoff_for_period_unknown_raises():
    try:
        cutoff_for_period("year")
    except ValueError as exc:
        assert "year" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown period")


def _sample_new_trailer():
    return {
        "youtube_video_id": "abc123",
        "trailer_name": "Avatar 3 | Official Trailer",
        "video_type": "Official Trailer",
        "content_kind": "movie",
        "title": "Avatar: Fire and Ash",
        "poster_path": "/poster.jpg",
        "trailer_published_at": datetime(2026, 5, 12, 14, 0, tzinfo=UTC),
        "release_date": date(2026, 12, 18),
        "view_count": 1234567,
        "like_count": 89000,
        "comment_count": 1500,
    }


def _sample_tracked_trailer():
    return {
        "youtube_video_id": "xyz789",
        "trailer_name": "Dune: Messiah | Teaser",
        "video_type": "Teaser",
        "content_kind": "movie",
        "title": "Dune: Messiah",
        "poster_path": "/dune.jpg",
        "trailer_published_at": "2026-04-30T09:00:00+00:00",
        "release_date": date(2026, 11, 20),
        "view_count": 9_000_000,
        "delta_views": 1_500_000,
        "like_count": 250_000,
        "delta_likes": 30_000,
    }


def _sample_country_row():
    return {
        "country": "US",
        "total_trailers": 312,
        "active_trailers": 220,
        "ended_trailers": 80,
        "unavailable_trailers": 12,
        "with_transcript": 180,
        "with_comments": 200,
    }


def test_render_includes_all_sections_and_data():
    html = render_digest_html(
        period="week",
        cutoff_date=date(2026, 5, 8),
        today=date(2026, 5, 15),
        new_trailers=[_sample_new_trailer()],
        top_tracked=[_sample_tracked_trailer()],
        country_stats=[_sample_country_row()],
        image_base="https://image.tmdb.org/t/p/w154",
        top_tracked_cap=50,
    )
    assert "weekly digest" in html
    assert "2026-05-08" in html
    assert "2026-05-15" in html
    # New trailers section
    assert "New trailers added" in html
    assert "Trailer Date" in html
    assert "Release Date" in html
    assert "Avatar: Fire and Ash" in html
    assert "https://www.youtube.com/watch?v=abc123" in html
    assert "https://image.tmdb.org/t/p/w154/poster.jpg" in html
    assert "1,234,567" in html
    assert "2026-05-12" in html  # trailer_published_at (datetime → date)
    assert "2026-12-18" in html  # release_date
    # Tracked section
    assert "Currently tracked" in html
    assert "most recent" in html
    assert "Dune: Messiah" in html
    assert "+1,500,000" in html
    assert "9,000,000" in html
    assert "2026-04-30" in html  # tracked trailer_published_at (ISO string → date)
    assert "2026-11-20" in html  # tracked release_date
    # Country section
    assert "Database statistics by country" in html
    assert ">US<" in html
    assert "312" in html


def test_render_handles_empty_sections():
    html = render_digest_html(
        period="month",
        cutoff_date=date(2026, 4, 15),
        today=date(2026, 5, 15),
        new_trailers=[],
        top_tracked=[],
        country_stats=[],
        image_base="https://image.tmdb.org/t/p/w154",
        top_tracked_cap=50,
    )
    assert "monthly digest" in html
    assert "No new trailers" in html
    assert "No tracked trailers" in html
    assert "No trailers in the database" in html


def test_render_escapes_html_in_titles():
    row = _sample_new_trailer()
    row["title"] = "<script>alert(1)</script>"
    html = render_digest_html(
        period="week",
        cutoff_date=date(2026, 5, 8),
        today=date(2026, 5, 15),
        new_trailers=[row],
        top_tracked=[],
        country_stats=[],
        image_base="https://image.tmdb.org/t/p/w154",
        top_tracked_cap=50,
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_render_handles_missing_poster_and_stats():
    row = _sample_new_trailer()
    row["poster_path"] = None
    row["view_count"] = None
    row["like_count"] = None
    row["trailer_published_at"] = None
    row["release_date"] = None
    html = render_digest_html(
        period="week",
        cutoff_date=date(2026, 5, 8),
        today=date(2026, 5, 15),
        new_trailers=[row],
        top_tracked=[],
        country_stats=[],
        image_base="https://image.tmdb.org/t/p/w154",
        top_tracked_cap=50,
    )
    # No img tag rendered when poster is missing.
    assert "image.tmdb.org/t/p/w154None" not in html
    # Em-dash placeholder for missing counts AND missing dates.
    assert "—" in html
