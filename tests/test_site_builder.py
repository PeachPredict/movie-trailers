"""Tests for the static-site data builder.

No BigQuery: the high-level query functions and the few raw `bq.query` calls are
stubbed, so this exercises the builder's shaping, file-writing, and graceful
degradation.
"""

from __future__ import annotations

import json
from datetime import date

from movie_trailers.site import builder


class FakeBQ:
    project = "p"
    dataset = "movie_trailers_dev"

    def query(self, sql, params=None):
        if "vw_movie_trailer_excitement" in sql and "GROUP BY movie_tmdb_id" in sql:
            return [{"movie_tmdb_id": 11, "n_trailers": 4}]
        if "FROM `p.movie_trailers_dev.movies`" in sql:
            return [{"tmdb_id": 11, "title": "Example Movie"}]
        if "trailer_comments_snapshots" in sql:
            return [{"youtube_video_id": "vid123", "movie_tmdb_id": 11}]
        if "COUNT(*) AS n FROM" in sql:
            return [{"n": 1992}]
        # view-timeseries: candidates, then per-movie series + launches
        if "WITH tr AS" in sql and "peak_views" in sql:
            return [
                {"movie_tmdb_id": 1, "n_trailers": 3, "first_pub": date(2026, 5, 1),
                 "peak_views": 500000, "days": 40, "title": "Multi Movie"},
                {"movie_tmdb_id": 2, "n_trailers": 1, "first_pub": date(2026, 5, 3),
                 "peak_views": 120000, "days": 30, "title": "Single Movie"},
            ]
        if "SUM(m.delta_views) AS dv" in sql:
            return [{"d": date(2026, 5, 10), "dv": 1000}, {"d": date(2026, 5, 11), "dv": 600}]
        if "SELECT DISTINCT DATE(published_at) AS d" in sql:
            return [{"d": date(2026, 5, 10)}]
        return []


class StubSettings:
    bq_dataset = "movie_trailers_dev"


def _patch_common(monkeypatch):
    q = builder.queries
    monkeypatch.setattr(q, "trending", lambda bq, **k: [
        {"youtube_video_id": "a", "title": "Has Poster", "poster_url": "https://img/x.jpg",
         "poster_path": "/x.jpg", "views_gained": 100, "likes_gained": 5,
         "view_count": 1000, "release_date": None, "content_kind": "movie"},
        {"youtube_video_id": "b", "title": "No Poster", "poster_url": None,
         "poster_path": None, "views_gained": 50},
    ])
    monkeypatch.setattr(q, "comment_excitement_decay", lambda bq, **k: {
        "model_version": "mlv1-test", "movies": 3,
        "by_movie_trailer_count": {"2": {}, "3-4": {}, "5+": {}}})
    monkeypatch.setattr(q, "engagement_decay", lambda bq, **k: {
        "unit": "movie", "days": 45, "units": 10, "losing": 6, "losing_share": 0.6,
        "median_pct_change": -0.09})
    monkeypatch.setattr(q, "movie_excitement_trend", lambda bq, **k: {
        "n_trailers": 4, "first_last_delta": -8.0, "slope_per_trailer": -2.0,
        "verdict": "cooling", "trailers": [
            {"trailer_ordinal": 1, "excitement": 70, "published_at": None, "youtube_video_id": "z"},
            {"trailer_ordinal": 2, "excitement": 62, "published_at": None, "youtube_video_id": "z"},
        ]})
    monkeypatch.setattr(q, "search_trailers", lambda bq, **k: [{"title": "t"}])
    monkeypatch.setattr(q, "trailer_metrics", lambda bq, **k: [{"view_count": 1}])
    monkeypatch.setattr(q, "top_comments", lambda bq, **k: [{"text": "x" * 500, "rank": 1}])
    monkeypatch.setattr(q, "movie_engagement_trend", lambda bq, **k: {"verdict": "ok"})
    monkeypatch.setattr(builder, "fetch_country_stats", lambda bq: [
        {"country": "US", "total_trailers": 100, "active_trailers": 80,
         "with_comments": 90, "with_transcript": 95},
        {"country": "UNKNOWN", "total_trailers": 5, "active_trailers": 1,
         "with_comments": 2, "with_transcript": 3},
    ])
    monkeypatch.setattr(builder, "detect_findings", lambda bq, **k: [])
    monkeypatch.setattr(builder, "track_record", lambda bq: {"hits": 1, "misses": 0})


def test_generate_site_data_happy_path(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(builder.queries, "_latest_excitement_version", lambda bq: "mlv1-test")

    written = builder.generate_site_data(FakeBQ(), StubSettings(), tmp_path)

    expected = {
        "carousel.json", "excitement_decay.json", "excitement_examples.json",
        "views_timeseries.json", "engagement_decay.json", "coverage.json",
        "mcp_examples.json", "meta.json",
    }
    assert set(written) == expected
    for name in expected:
        json.loads((tmp_path / name).read_text())  # all valid JSON

    views = json.loads((tmp_path / "views_timeseries.json").read_text())
    assert [m["n_trailers"] for m in views["movies"]] == [3, 1]  # multi first, single second
    assert views["movies"][0]["launches"]  # launch dates snapped onto the series

    carousel = json.loads((tmp_path / "carousel.json").read_text())
    assert len(carousel) == 1  # the poster-less row is dropped
    assert "poster_path" not in carousel[0]  # trimmed

    mcp = json.loads((tmp_path / "mcp_examples.json").read_text())
    assert len(mcp["tools"]) == 10
    assert all("name" in t and "example_response" in t for t in mcp["tools"])
    # comment text truncated to the cap
    tc = next(t for t in mcp["tools"] if t["name"] == "top_comments")
    assert len(tc["example_response"][0]["text"]) <= builder._TEXT_CAP + 1

    coverage = json.loads((tmp_path / "coverage.json").read_text())
    assert coverage["unique_trailers"] == 1992
    assert coverage["countries_covered"] == 1  # UNKNOWN excluded

    meta = json.loads((tmp_path / "meta.json").read_text())
    assert "generated_at" in meta and meta["model_version"] == "mlv1-test"


def test_generate_site_data_no_excitement(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(builder.queries, "_latest_excitement_version", lambda bq: None)

    builder.generate_site_data(FakeBQ(), StubSettings(), tmp_path)

    examples = json.loads((tmp_path / "excitement_examples.json").read_text())
    assert examples["model_version"] is None
    assert examples["examples"] == []  # degrades gracefully
