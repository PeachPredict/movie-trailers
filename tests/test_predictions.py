"""Prediction-log logic that doesn't need BigQuery: horizons and the scoreboard."""
from datetime import date
from unittest.mock import MagicMock

from movie_trailers.content.findings import Finding, trailer_due_horizon
from movie_trailers.content.predictions import record_predictions, track_record


def _finding(tmdb_id: int, d2r: int) -> Finding:
    return Finding(
        kind="trailer_due", movie_tmdb_id=tmdb_id, title=f"m{tmdb_id}",
        headline="h", salience=1.0, metrics={"days_to_release": d2r, "vs_peak": 0.1},
        series=[],
    )


def test_trailer_due_horizon_is_capped_at_release():
    assert trailer_due_horizon(40) == 21
    assert trailer_due_horizon(9) == 9
    assert trailer_due_horizon(None) == 21
    assert trailer_due_horizon(0) == 21


def test_record_predictions_caps_horizon_and_skips_open_movies():
    bq = MagicMock()
    bq.query.return_value = [{"movie_tmdb_id": 2}]  # movie 2 already has an open call
    record_predictions(bq, [_finding(1, 9), _finding(2, 30), _finding(3, 30)], today=date(2026, 10, 11))
    rows = bq.merge_rows.call_args.kwargs["rows"]
    assert [r.movie_tmdb_id for r in rows] == [1, 3]
    assert [r.horizon_days for r in rows] == [9, 21]
    assert rows[0].prediction_id == "1-2026-10-11"


def test_track_record_totals_and_by_kind():
    bq = MagicMock()
    bq.query.return_value = [
        {"kind": "opening_tier", "hits": 1, "misses": 1, "open": 2, "avg_hit_lag_days": None},
        {"kind": "trailer_due", "hits": 3, "misses": 1, "open": 5, "avg_hit_lag_days": 4.5},
    ]
    t = track_record(bq)
    assert (t["hits"], t["misses"], t["open"]) == (4, 2, 7)
    assert t["hit_rate"] == 4 / 6
    assert t["avg_hit_lag_days"] == 4.5
    assert t["by_kind"]["trailer_due"]["hit_rate"] == 0.75
    assert t["by_kind"]["opening_tier"]["open"] == 2


def test_track_record_empty_table():
    bq = MagicMock()
    bq.query.return_value = []
    t = track_record(bq)
    assert t["hit_rate"] is None and t["by_kind"] == {} and t["open"] == 0
