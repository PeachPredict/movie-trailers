"""The prediction log: persist trailer-due calls, resolve them, score the record.

A `trailer_due` finding is a public, falsifiable prediction ("a new trailer is
imminent"). This module logs each one, then on later runs checks whether a new
trailer actually dropped within the horizon — turning the calls into a hit-rate
you can post. Writes go through the same idempotent BigQuery helpers as the
pipeline; one open prediction per movie at a time.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.content.findings import Finding
from movie_trailers.models import ContentPredictionRow

HORIZON_DAYS = 21  # a trailer must land within this many days for a 'hit'

_FIELDS = [
    "prediction_id", "kind", "movie_tmdb_id", "title", "predicted_at", "horizon_days",
    "days_to_release_at_prediction", "vs_peak_at_prediction", "basis", "status",
    "resolved_at", "resolved_youtube_video_id", "resolved_lag_days",
    "created_at", "updated_at",
]


def _table(bq: BigQueryClient, name: str) -> str:
    return f"`{bq.project}.{bq.dataset}.{name}`"


def open_predictions(bq: BigQueryClient) -> list[dict[str, Any]]:
    """All predictions still awaiting resolution."""
    return bq.query(
        f"SELECT * FROM {_table(bq, 'content_predictions')} WHERE status = 'open'"
    )


def record_predictions(
    bq: BigQueryClient,
    findings: list[Finding],
    *,
    today: date,
    horizon_days: int = HORIZON_DAYS,
) -> int:
    """Open a new prediction for each `trailer_due` finding lacking an open one.

    Idempotent: re-running on the same day merges to the same prediction_id, and
    a movie that already has an open prediction is skipped (no duplicate episode).
    Returns the number of predictions newly written.
    """
    has_open = {p["movie_tmdb_id"] for p in open_predictions(bq)}
    now = datetime.now(UTC)
    rows: list[ContentPredictionRow] = []
    for f in findings:
        if f.kind != "trailer_due" or f.movie_tmdb_id in has_open:
            continue
        rows.append(
            ContentPredictionRow(
                prediction_id=f"{f.movie_tmdb_id}-{today.isoformat()}",
                kind=f.kind,
                movie_tmdb_id=f.movie_tmdb_id,
                title=f.title,
                predicted_at=today,
                horizon_days=horizon_days,
                days_to_release_at_prediction=f.metrics.get("days_to_release"),
                vs_peak_at_prediction=f.metrics.get("vs_peak"),
                basis=f.headline,
                status="open",
                created_at=now,
                updated_at=now,
            )
        )
    if not rows:
        return 0
    bq.merge_rows(
        table="content_predictions",
        rows=rows,
        merge_keys=["prediction_id"],
        update_fields=[c for c in _FIELDS if c != "prediction_id"],
        insert_fields=_FIELDS,
    )
    return len(rows)


def resolve_predictions(
    bq: BigQueryClient, *, today: date
) -> dict[str, int]:
    """Resolve open predictions: 'hit' if a new trailer landed in window, else 'miss'.

    A hit = the movie published a trailer strictly after predicted_at and within
    horizon_days. A miss = the horizon has fully elapsed with no such trailer.
    Predictions still inside their window stay open. Returns {hits, misses}.
    """
    preds = open_predictions(bq)
    if not preds:
        return {"hits": 0, "misses": 0}

    # Inline the integer movie ids — the BigQuery client param helper has no
    # array support, and these are our own INT64 ids (no injection surface).
    ids_sql = ", ".join(str(int(p["movie_tmdb_id"])) for p in preds)
    trailers = bq.query(
        f"""
        SELECT movie_tmdb_id, youtube_video_id, DATE(published_at) AS pub
        FROM {_table(bq, 'trailers')}
        WHERE content_kind = 'movie'
          AND movie_tmdb_id IN ({ids_sql})
          AND published_at IS NOT NULL
        ORDER BY published_at
        """
    )
    by_movie: dict[int, list[dict[str, Any]]] = {}
    for t in trailers:
        by_movie.setdefault(t["movie_tmdb_id"], []).append(t)

    now = datetime.now(UTC)
    updates: list[dict[str, Any]] = []
    hits = misses = 0
    for p in preds:
        pred_at: date = p["predicted_at"]
        deadline_passed = (today - pred_at).days > p["horizon_days"]
        # earliest trailer published after the prediction
        new_trailer = next(
            (t for t in by_movie.get(p["movie_tmdb_id"], []) if t["pub"] > pred_at),
            None,
        )
        if new_trailer:
            lag = (new_trailer["pub"] - pred_at).days
            if lag <= p["horizon_days"]:
                updates.append(
                    {
                        "prediction_id": p["prediction_id"],
                        "status": "hit",
                        "resolved_at": today.isoformat(),
                        "resolved_youtube_video_id": new_trailer["youtube_video_id"],
                        "resolved_lag_days": lag,
                        "updated_at": now.isoformat(),
                    }
                )
                hits += 1
                continue
        if deadline_passed:
            updates.append(
                {
                    "prediction_id": p["prediction_id"],
                    "status": "miss",
                    "resolved_at": today.isoformat(),
                    "resolved_youtube_video_id": None,
                    "resolved_lag_days": None,
                    "updated_at": now.isoformat(),
                }
            )
            misses += 1

    if updates:
        bq.update_from_dicts(
            table="content_predictions",
            rows=updates,
            merge_keys=["prediction_id"],
            update_clause_sql=(
                "T.status = S.status, T.resolved_at = S.resolved_at, "
                "T.resolved_youtube_video_id = S.resolved_youtube_video_id, "
                "T.resolved_lag_days = S.resolved_lag_days, T.updated_at = S.updated_at"
            ),
        )
    return {"hits": hits, "misses": misses}


def track_record(bq: BigQueryClient) -> dict[str, Any]:
    """Aggregate hit/miss/open counts, hit rate, and average lag-to-hit."""
    rows = bq.query(
        f"""
        SELECT
          COUNTIF(status = 'hit')  AS hits,
          COUNTIF(status = 'miss') AS misses,
          COUNTIF(status = 'open') AS open,
          AVG(IF(status = 'hit', resolved_lag_days, NULL)) AS avg_hit_lag_days
        FROM {_table(bq, 'content_predictions')}
        """
    )
    r = rows[0] if rows else {}
    hits, misses = r.get("hits", 0) or 0, r.get("misses", 0) or 0
    resolved = hits + misses
    return {
        "hits": hits,
        "misses": misses,
        "open": r.get("open", 0) or 0,
        "hit_rate": (hits / resolved) if resolved else None,
        "avg_hit_lag_days": r.get("avg_hit_lag_days"),
    }
