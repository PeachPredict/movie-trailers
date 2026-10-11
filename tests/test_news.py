"""GDELT news signal: target building, normalisation, and the media_surge detector."""
from datetime import date
from unittest.mock import MagicMock

from movie_trailers.content import findings as fd
from movie_trailers.pipeline.news import gkg_norm, run_news, targets_from


def test_gkg_norm_matches_sql_rule():
    assert gkg_norm("Drishyam 3: The Conclusion") == "drishyam 3 the conclusion"
    assert gkg_norm("Toxic : A Fairy Tale for Grown-ups") == "toxic a fairy tale for grown ups"
    assert gkg_norm("Pokémon") == "pokémon"  # SQL LOWER keeps accents; so do we
    assert gkg_norm("ദൃശ്യം 3") == "ദൃശ്യം 3"  # combining marks kept


def test_targets_from_titles_people_and_short_flag():
    movies = [
        {"tmdb_id": 1, "title": "Hope", "original_title": "호프"},
        {"tmdb_id": 2, "title": "Drishyam 3: The Conclusion", "original_title": "ദൃശ്യം 3"},
        {"tmdb_id": 3, "title": "XO", "original_title": "XO"},  # kept, but short → needs a person
    ]
    people = [{"tmdb_id": 1, "name": "Hwang Jung-min"}, {"tmdb_id": 2, "name": "Mohanlal"},
              {"tmdb_id": 9, "name": "Nobody"}]
    rows, short = targets_from(movies, people)
    assert short == {1: True, 2: False, 3: True}
    titles = {(r.tmdb_id, r.name) for r in rows if r.kind == "title"}
    assert titles == {(1, "hope"), (1, "호프"), (2, "drishyam 3 the conclusion"), (2, "ദൃശ്യം 3"), (3, "xo")}
    persons = {(r.tmdb_id, r.name) for r in rows if r.kind == "person"}
    assert persons == {(1, "hwang jung min"), (2, "mohanlal")}


def test_run_news_uses_confirmed_for_short_titles():
    from movie_trailers.config import Settings

    bq = MagicMock()
    bq.query.side_effect = [
        [{"tmdb_id": 1, "title": "Hope", "original_title": "Hope"},
         {"tmdb_id": 2, "title": "Drishyam 3: The Conclusion", "original_title": "ദൃശ്യം 3"}],
        [{"tmdb_id": 1, "name": "Hwang Jung-min"}],
        [{"tmdb_id": 1, "articles": 500, "confirmed_articles": 7, "sources": 300, "avg_tone": 1.2, "top_sources": ["a"]},
         {"tmdb_id": 2, "articles": 40, "confirmed_articles": 3, "sources": 20, "avg_tone": 2.0, "top_sources": None}],
    ]
    days, n = run_news(bq=bq, settings=Settings(GCP_PROJECT="p"), today=date(2026, 10, 11), days_back=1)  # type: ignore[call-arg]
    assert (days, n) == (1, 2)
    gdelt_call = bq.query.call_args_list[2]
    assert gdelt_call.kwargs["location"] == "US"  # GDELT lives in the US multi-region
    assert gdelt_call.args[1]["title_names"] == ["hope", "drishyam 3 the conclusion", "ദൃശ്യം 3"] or set(gdelt_call.args[1]["title_names"]) == {"hope", "drishyam 3 the conclusion", "ദൃശ്യം 3"}
    assert gdelt_call.args[1]["person_ids"] == ["1"]
    rows = {r.tmdb_id: r for r in bq.merge_rows.call_args.kwargs["rows"]}
    assert rows[1].articles_used == 7 and rows[2].articles_used == 40
    assert rows[1].date == date(2026, 10, 10) and rows[2].top_sources == []
    assert "T.date BETWEEN" in bq.merge_rows.call_args.kwargs["partition_filter"]


def _news_row(tmdb_id, recent, base, d2r, tone=1.0):
    return {"movie_tmdb_id": tmdb_id, "title": f"m{tmdb_id}", "recent": recent, "base": base,
            "tone": tone, "days_to_release": d2r,
            "series": [{"date": date(2026, 10, 1), "dv": base}, {"date": date(2026, 10, 10), "dv": recent}]}


def test_media_surge_detector(monkeypatch):
    bq = MagicMock()
    monkeypatch.setattr(fd, "_scan", lambda bq, days: [])
    monkeypatch.setattr(fd, "_scan_excitement", lambda bq: [])
    monkeypatch.setattr(fd, "_scan_news", lambda bq: [
        _news_row(1, recent=60.0, base=10.0, d2r=20),   # 6× → surge
        _news_row(2, recent=60.0, base=30.0, d2r=20),   # 2× → no
        _news_row(3, recent=15.0, base=1.0, d2r=20),    # too few articles
        _news_row(4, recent=60.0, base=10.0, d2r=120),  # too far from release
        _news_row(5, recent=60.0, base=None, d2r=5),    # from nothing → surge
    ])
    out = fd.detect_findings(bq, days=30, top=10)
    assert {f.movie_tmdb_id for f in out} == {1, 5}
    f1 = next(f for f in out if f.movie_tmdb_id == 1)
    assert f1.kind == "media_surge" and "6.0×" in f1.headline and f1.metrics["news_ratio"] == 6.0
    f5 = next(f for f in out if f.movie_tmdb_id == 5)
    assert "from nothing" in f5.headline and f5.metrics["news_ratio"] is None
