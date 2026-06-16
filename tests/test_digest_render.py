from datetime import date

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


def test_render_includes_country_section():
    html = render_digest_html(
        period="week",
        today=date(2026, 5, 15),
        country_stats=[_sample_country_row()],
    )
    assert "weekly digest" in html
    assert "2026-05-15" in html
    assert "Database statistics by country" in html
    assert ">US<" in html
    assert "312" in html
    assert "180" in html  # with_transcript


def test_render_period_monthly_label():
    html = render_digest_html(
        period="month",
        today=date(2026, 5, 15),
        country_stats=[_sample_country_row()],
    )
    assert "monthly digest" in html


def test_render_handles_empty_country_stats():
    html = render_digest_html(
        period="week",
        today=date(2026, 5, 15),
        country_stats=[],
    )
    assert "No trailers in the database" in html


def test_render_escapes_html_in_country_name():
    row = _sample_country_row()
    row["country"] = "<script>alert(1)</script>"
    html = render_digest_html(
        period="week",
        today=date(2026, 5, 15),
        country_stats=[row],
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
