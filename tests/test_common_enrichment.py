from datetime import UTC, datetime

from movie_trailers.pipeline._common import (
    credits_from_details,
    parse_iso8601_duration,
    parse_origin_countries,
    watch_providers_from_details,
)

NOW = datetime(2026, 5, 12, 14, 0, tzinfo=UTC)


def test_parse_iso8601_duration_handles_hours_minutes_seconds():
    assert parse_iso8601_duration("PT2M30S") == 150
    assert parse_iso8601_duration("PT1H5M10S") == 3910
    assert parse_iso8601_duration("PT45S") == 45
    assert parse_iso8601_duration("PT2H") == 7200


def test_parse_iso8601_duration_none_on_bad_input():
    assert parse_iso8601_duration(None) is None
    assert parse_iso8601_duration("") is None
    assert parse_iso8601_duration("2:30") is None
    assert parse_iso8601_duration("PTabc") is None


def test_parse_origin_countries_tv_uses_origin_country():
    assert parse_origin_countries({"origin_country": ["US", "GB"]}) == ["US", "GB"]


def test_parse_origin_countries_movie_uses_production_countries():
    details = {"production_countries": [{"iso_3166_1": "US"}, {"iso_3166_1": "FR"}]}
    assert parse_origin_countries(details) == ["US", "FR"]


def test_watch_providers_flattens_regions_and_kinds():
    details = {
        "watch/providers": {
            "results": {
                "US": {
                    "link": "https://justwatch.com/us/movie/x",
                    "flatrate": [
                        {"provider_id": 8, "provider_name": "Netflix", "display_priority": 0}
                    ],
                    "rent": [
                        {"provider_id": 2, "provider_name": "Apple TV", "display_priority": 1}
                    ],
                },
                "GB": {
                    "link": "https://justwatch.com/uk/movie/x",
                    "flatrate": [
                        {"provider_id": 9, "provider_name": "Prime Video", "display_priority": 0}
                    ],
                },
            }
        }
    }
    rows = watch_providers_from_details(
        details, tmdb_id=42, content_kind="movie", now=NOW
    )
    assert len(rows) == 3
    keys = {(r.region, r.kind, r.provider_id) for r in rows}
    assert ("US", "flatrate", 8) in keys
    assert ("US", "rent", 2) in keys
    assert ("GB", "flatrate", 9) in keys
    # Link is carried through.
    us_row = next(r for r in rows if r.region == "US" and r.kind == "flatrate")
    assert us_row.link.endswith("/movie/x")


def test_watch_providers_empty_when_block_missing():
    assert watch_providers_from_details(
        {}, tmdb_id=1, content_kind="movie", now=NOW
    ) == []


def test_credits_returns_top_cast_and_dedupes_crew_jobs():
    details = {
        "credits": {
            "cast": [
                {"id": i, "name": f"Actor {i}", "character": f"Char {i}", "order": i}
                for i in range(15)
            ],
            "crew": [
                {"id": 99, "name": "Jane Doe", "job": "Director", "department": "Directing"},
                {"id": 99, "name": "Jane Doe", "job": "Writer", "department": "Writing"},
                {"id": 100, "name": "Bob", "job": "Producer", "department": "Production"},
                {"id": 101, "name": "Skip", "job": "Foley Artist", "department": "Sound"},
            ],
        }
    }
    rows = credits_from_details(details, tmdb_id=1, content_kind="movie", now=NOW)
    cast = [r for r in rows if r.credit_kind == "cast"]
    crew = [r for r in rows if r.credit_kind == "crew"]
    assert len(cast) == 10
    assert {r.person_id for r in crew} == {99, 100}  # Foley Artist filtered out
    jane = next(r for r in crew if r.person_id == 99)
    assert "Director" in jane.job and "Writer" in jane.job
