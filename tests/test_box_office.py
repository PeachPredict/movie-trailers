"""Box-office outcomes: OMDb parsing, ANCINE parsing/matching, opening-tier logic, rendering."""
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from movie_trailers.clients.omdb import parse_box_office
from movie_trailers.content import predictions as pr
from movie_trailers.content.render import render_content_section
from movie_trailers.pipeline import ancine
from movie_trailers.pipeline.ancine import (
    TitleIndex,
    film_week,
    map_columns,
    parse_ancine_csv,
    parse_date,
    parse_number,
)

TODAY = date(2026, 10, 11)


# --- OMDb ----------------------------------------------------------------------------

def test_parse_box_office():
    assert parse_box_office("$389,804,217") == 389804217
    assert parse_box_office("N/A") is None
    assert parse_box_office("") is None
    assert parse_box_office(None) is None


# --- ANCINE parsing -------------------------------------------------------------------

def test_parse_number_and_date_brazilian_formats():
    assert parse_number("1.234.567") == 1234567
    assert parse_number("12.345,67") == 12345.67
    assert parse_number("R$ 1.000,00") == 1000.0
    assert parse_number("987") == 987
    assert parse_number("") is None
    assert parse_date("16/04/2026") == date(2026, 4, 16)
    assert parse_date("2026-04-16") == date(2026, 4, 16)


def test_film_week_runs_thursday_to_wednesday():
    assert film_week(date(2026, 10, 8)) == (date(2026, 10, 8), date(2026, 10, 14))  # Thursday
    assert film_week(date(2026, 10, 14)) == (date(2026, 10, 8), date(2026, 10, 14))  # Wednesday
    assert film_week(date(2026, 10, 12)) == (date(2026, 10, 8), date(2026, 10, 14))  # Monday


def test_map_columns_is_accent_insensitive():
    cols = map_columns(["Título original da obra", "Título no Brasil", "País", "Data Inicial da Exibição",
                        "Data Final da Exibição", "Público", "Renda"])
    assert cols["raw_title"] == 0 and cols["period_end"] == 4 and cols["admissions"] == 5


def test_parse_weekly_layout(tmp_path: Path):
    f = tmp_path / "semanal.csv"
    f.write_text(
        "Título original da obra;Título no Brasil;País;Data Inicial da Exibição;Data Final da Exibição;Público;Renda\n"
        "AINDA ESTOU AQUI;Ainda Estou Aqui;BRASIL;07/11/2024;13/11/2024;512.345;9.876.543,21\n"
        "GLADIATOR II;Gladiador II;ESTADOS UNIDOS;14/11/2024;20/11/2024;300.000;\n",
        encoding="latin-1",
    )
    recs = list(parse_ancine_csv(f))
    assert [r.raw_title for r in recs] == ["AINDA ESTOU AQUI", "GLADIATOR II"]
    assert recs[0].admissions == 512345 and recs[0].revenue_brl == 9876543.21
    assert recs[0].period_start == date(2024, 11, 7) and recs[0].period_end == date(2024, 11, 13)
    assert recs[1].revenue_brl is None


def test_parse_daily_layout_aggregates_into_film_weeks(tmp_path: Path):
    f = tmp_path / "dia.csv"
    f.write_text(
        "Data da Exibição;CPB/ROE;Título original da obra;Título no Brasil;País;Nome Sala;Público\n"
        "09/10/2026;X;DRISHYAM 3;Drishyam 3;INDIA;Sala 1;100\n"
        "10/10/2026;X;DRISHYAM 3;Drishyam 3;INDIA;Sala 2;50\n"
        "15/10/2026;X;DRISHYAM 3;Drishyam 3;INDIA;Sala 1;20\n",
        encoding="utf-8",
    )
    recs = sorted(parse_ancine_csv(f), key=lambda r: r.period_end)
    assert [(r.period_end, r.admissions) for r in recs] == [
        (date(2026, 10, 14), 150.0), (date(2026, 10, 21), 20.0),
    ]
    assert recs[0].revenue_brl is None


# --- ANCINE matching ------------------------------------------------------------------

def _movies():
    return [
        {"tmdb_id": 1, "title": "I'm Still Here", "original_title": "Ainda Estou Aqui",
         "primary_release_date": date(2024, 11, 7)},
        {"tmdb_id": 2, "title": "The Train", "original_title": "The Train",
         "primary_release_date": date(2026, 9, 1)},
        {"tmdb_id": 3, "title": "The Train", "original_title": "The Train",
         "primary_release_date": date(2019, 1, 1)},  # too old for a 2026 week
    ]


def test_title_index_prefers_release_closest_to_the_week():
    idx = TitleIndex(_movies())
    assert idx.match("AINDA ESTOU AQUI", date(2024, 11, 13))["tmdb_id"] == 1
    assert idx.match("THE TRAIN", date(2026, 10, 14))["tmdb_id"] == 2
    assert idx.match("Nope", date(2026, 10, 14)) is None


def test_load_ancine_matches_and_writes(tmp_path: Path, monkeypatch):
    f = tmp_path / "semanal.csv"
    f.write_text(
        "Título original da obra;Título no Brasil;País;Data Inicial da Exibição;Data Final da Exibição;Público;Renda\n"
        "AINDA ESTOU AQUI;Ainda Estou Aqui;BRASIL;07/11/2024;13/11/2024;512345;1000\n"
        "UNKNOWN FILM;Filme Desconhecido;BRASIL;07/11/2024;13/11/2024;10;\n",
        encoding="utf-8",
    )
    bq = MagicMock()
    bq.query.return_value = _movies()
    tmdb = MagicMock()
    tmdb.search_movie.return_value = []  # the unknown title stays unmatched
    summary = ancine.load_ancine(bq=bq, tmdb=tmdb, source=str(f), today=TODAY)
    assert summary["matched"] == 1 and summary["unmatched"] == ["UNKNOWN FILM"]
    rows = bq.merge_rows.call_args.kwargs["rows"]
    assert {(r.metric, r.value) for r in rows} == {("admissions", 512345.0), ("revenue_brl", 1000.0)}
    assert rows[0].tmdb_id == 1 and rows[0].cumulative is False and rows[0].match_method == "original_title"
    assert tmdb.search_movie.call_args.args[0] == "Filme Desconhecido"


def test_tmdb_search_match_requires_original_title_and_tracked_id():
    idx = TitleIndex(_movies())
    tmdb = MagicMock()
    rec = ancine.AncineRecord("AINDA ESTOU AQUI", "Ainda Estou Aqui", "BRASIL", TODAY, TODAY, 1, None)
    tmdb.search_movie.return_value = [{"id": 999, "original_title": "Ainda Estou Aqui"},
                                      {"id": 1, "original_title": "Ainda Estou Aqui"}]
    assert ancine._tmdb_search_match(tmdb, idx, rec)["tmdb_id"] == 1
    tmdb.search_movie.return_value = [{"id": 1, "original_title": "Something Else"}]
    assert ancine._tmdb_search_match(tmdb, idx, rec) is None


# --- opening tier ---------------------------------------------------------------------

def test_tier_of_uses_rank_within_cohort():
    cohort = [float(i) for i in range(1, 101)]
    assert pr.tier_of(95.5, cohort) == "high"
    assert pr.tier_of(60.5, cohort) == "mid"
    assert pr.tier_of(10.5, cohort) == "low"
    assert pr.tier_of(5.0, []) == "low"


def test_record_opening_tier_skips_small_cohort_and_known_movies():
    bq = MagicMock()
    bq.query.return_value = [{"total_views": 10.0}] * 5  # cohort too small
    assert pr.record_opening_tier(bq, today=TODAY) == 0
    bq.merge_rows.assert_not_called()


def test_record_opening_tier_writes_one_call_per_movie():
    bq = MagicMock()
    cohort = [{"total_views": float(v)} for v in range(1000, 1000 + 40 * 1000, 1000)]  # 1k..40k
    bq.query.side_effect = [
        cohort,
        [{"movie_tmdb_id": 7}],  # already called
        [
            {"tmdb_id": 7, "title": "Known", "rel": TODAY, "d2r": 3, "total_views": 50000, "peak_daily_views": 5000, "n_days": 10},
            {"tmdb_id": 8, "title": "Big", "rel": TODAY, "d2r": 5, "total_views": 39500, "peak_daily_views": 5000, "n_days": 10},
            {"tmdb_id": 9, "title": "Tiny", "rel": TODAY, "d2r": 5, "total_views": 3000, "peak_daily_views": 100, "n_days": 10},
            {"tmdb_id": 10, "title": "Low", "rel": TODAY, "d2r": 6, "total_views": 2500, "peak_daily_views": 2000, "n_days": 4},
        ],
    ]
    assert pr.record_opening_tier(bq, today=TODAY) == 2
    rows = bq.merge_rows.call_args.kwargs["rows"]
    assert [(r.movie_tmdb_id, r.predicted_value) for r in rows] == [(8, "high"), (10, "low")]
    assert rows[0].prediction_id == "8-2026-10-11-opening" and rows[0].kind == "opening_tier"
    assert "top 2%" in rows[0].basis


def _open(pid, tmdb_id, tier):
    return {"prediction_id": pid, "kind": "opening_tier", "movie_tmdb_id": tmdb_id,
            "predicted_value": tier, "status": "open"}


def _outcome_rows(n=30, market="BR", metric="admissions", rel=date(2026, 8, 1)):
    return [{"tmdb_id": 100 + i, "market": market, "metric": metric, "value": float(1000 * (i + 1)),
             "through_date": rel + timedelta(days=60), "rel": rel} for i in range(n)]


def test_resolve_opening_tier_scores_hit_miss_and_unscorable():
    bq = MagicMock()
    rel = date(2026, 8, 1)
    outcomes = _outcome_rows(rel=rel) + [
        {"tmdb_id": 1, "market": "BR", "metric": "admissions", "value": 29500.0,
         "through_date": rel + timedelta(days=40), "rel": rel},
        {"tmdb_id": 2, "market": "BR", "metric": "admissions", "value": 1500.0,
         "through_date": rel + timedelta(days=40), "rel": rel},
        {"tmdb_id": 3, "market": "WW", "metric": "revenue_usd", "value": 5.0,  # too fresh for WW
         "through_date": rel + timedelta(days=10), "rel": rel},
    ]
    bq.query.side_effect = [
        [_open("1-x", 1, "high"), _open("2-x", 2, "high"), _open("3-x", 3, "mid"), _open("4-x", 4, "low")],
        [{"tmdb_id": 1, "primary_release_date": rel}, {"tmdb_id": 2, "primary_release_date": rel},
         {"tmdb_id": 3, "primary_release_date": rel}, {"tmdb_id": 4, "primary_release_date": date(2026, 5, 1)}],
        outcomes,
    ]
    counts = pr.resolve_opening_tier(bq, today=TODAY)
    assert counts == {"hits": 1, "misses": 1, "unscorable": 1}
    ups = {u["prediction_id"]: u for u in bq.update_from_dicts.call_args.kwargs["rows"]}
    assert ups["1-x"]["status"] == "hit" and ups["1-x"]["observed_market"] == "BR"
    assert ups["2-x"]["status"] == "miss" and ups["2-x"]["observed_value"] == 1500.0
    assert ups["4-x"]["observed_metric"] == "none"  # released 2026-05-01: expired, no data
    assert "3-x" not in ups  # WW number not mature yet → still open


def test_resolved_since_and_render_blocks():
    resolved = [
        {"kind": "trailer_due", "title": "Hope", "status": "hit", "resolved_at": date(2026, 10, 9),
         "predicted_at": date(2026, 10, 1), "horizon_days": 14, "days_to_release_at_prediction": 14,
         "resolved_lag_days": 8, "resolved_trailer_name": "Main Trailer"},
        {"kind": "opening_tier", "title": "Big", "status": "miss", "resolved_at": date(2026, 10, 9),
         "predicted_at": date(2026, 8, 1), "predicted_value": "high", "signal_value": 2_500_000,
         "observed_value": 120_000, "observed_metric": "admissions", "observed_market": "BR"},
        {"kind": "opening_tier", "title": "Ghost", "status": "miss", "resolved_at": date(2026, 10, 9),
         "predicted_at": date(2026, 5, 1), "predicted_value": "mid", "observed_metric": "none"},
    ]
    outcomes = [{"title": "Big", "countries": "BR", "release_date": date(2026, 8, 7), "total_views": 2_500_000,
                 "peak_daily_views": 300_000, "br_admissions": 120_000, "us_gross_usd": None,
                 "ww_revenue_usd": 4_200_000, "predicted_tier": "high", "prediction_status": "miss"}]
    track = {"hits": 1, "misses": 1, "open": 2, "unscorable": 1, "hit_rate": 0.5, "avg_hit_lag_days": 8.0,
             "by_kind": {"trailer_due": {"hits": 1, "misses": 0, "open": 1, "unscorable": 0, "hit_rate": 1.0,
                                         "avg_hit_lag_days": 8.0},
                         "opening_tier": {"hits": 0, "misses": 1, "open": 1, "unscorable": 1, "hit_rate": 0.0,
                                          "avg_hit_lag_days": None}}}
    html = render_content_section([], track=track, resolved=resolved, outcomes=outcomes, cutoff=date(2026, 10, 4))
    assert "Resolved since 2026-10-04" in html
    assert "Main Trailer" in html and "landed after 8d" in html
    assert "BR admissions 120,000" in html and "✅ hit" in html and "❌ miss" in html and "⚪ n/a" in html
    assert "Trailer signal vs box office" in html and "$4.2M" in html and "2.5M" in html
    assert "opening tier:" in html and "1 unscorable" in html


def test_render_without_outcome_sections_is_unchanged_shape():
    html = render_content_section([], track={"hits": 0, "misses": 0, "open": 0, "hit_rate": None})
    assert "Resolved" not in html and "box office" not in html


def test_box_office_phase_writes_tmdb_and_omdb_rows(monkeypatch):
    from movie_trailers.config import Settings
    from movie_trailers.pipeline import box_office as bo

    bq = MagicMock()
    bq.query.return_value = [{"tmdb_id": 5, "imdb_id": "tt1", "title": "A", "primary_release_date": TODAY, "popularity": 9}]
    tmdb = MagicMock()
    tmdb.movie_box_office.return_value = {"revenue": 1234, "budget": 100}
    omdb = MagicMock()
    omdb.domestic_gross.return_value = 999
    settings = Settings(GCP_PROJECT="p")  # type: ignore[call-arg]
    checked, written = bo.run_box_office(tmdb=tmdb, omdb=omdb, bq=bq, settings=settings, today=TODAY)
    assert (checked, written) == (1, 2)
    rows = bq.merge_rows.call_args.kwargs["rows"]
    assert {(r.source, r.market, r.metric, r.value) for r in rows} == {
        ("tmdb", "WW", "revenue_usd", 1234.0), ("omdb", "US", "domestic_gross_usd", 999.0),
    }
    assert rows[0].budget_usd == 100 and rows[0].cumulative is True and rows[0].period_end == TODAY
    assert isinstance(rows[0].observed_at, datetime)
