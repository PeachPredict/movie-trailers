"""Box-office phase: outcome numbers for recently released movies.

Two API sources, both best-effort per title:
- TMDB `budget` / `revenue` (lifetime worldwide USD; crowd-sourced, fills in over
  weeks) → `tmdb / WW / revenue_usd`.
- OMDb `BoxOffice` by IMDb id (US domestic gross from Box Office Mojo) →
  `omdb / US / domestic_gross_usd`. Needs `OMDB_API_KEY`; 1,000 calls/day free.

Brazilian admissions come from a file, not an API: see `pipeline/ancine.py`.

Candidates are tracked movies (ones that have a trailer row) released between 1 and
`box_office_max_age_days` days ago, most popular first, each re-observed at most once
per `box_office_refresh_days`. Rows MERGE on (tmdb_id, source, market, metric,
period_end) — a re-run on the same day is a no-op.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import structlog

from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.clients.omdb import OMDbAuthError, OMDbClient
from movie_trailers.clients.tmdb import TMDBClient
from movie_trailers.config import Settings
from movie_trailers.models import BoxOfficeObservationRow

log = structlog.get_logger()

_FIELDS = list(BoxOfficeObservationRow.model_fields.keys())
MERGE_KEYS = ["tmdb_id", "source", "market", "metric", "period_end"]


def run_box_office(
    *,
    tmdb: TMDBClient,
    omdb: OMDbClient | None,
    bq: BigQueryClient,
    settings: Settings,
    today: date | None = None,
    limit: int | None = None,
) -> tuple[int, int]:
    """Observe TMDB revenue (+ OMDb domestic gross) for released movies.

    Returns (movies_checked, observations_written).
    """
    today = today or datetime.now(UTC).date()
    cap = settings.box_office_max_per_run if limit is None else min(limit, settings.box_office_max_per_run)
    movies = _select_candidates(bq, settings, today, cap=cap)
    if not movies:
        log.info("box_office.no_candidates")
        return 0, 0

    now = datetime.now(UTC)
    rows: list[BoxOfficeObservationRow] = []
    omdb_calls = 0
    omdb_open = omdb is not None
    for m in movies:
        tmdb_id = int(m["tmdb_id"])
        try:
            d = tmdb.movie_box_office(tmdb_id)
        except Exception as exc:  # noqa: BLE001 — one bad title must not stop the run
            log.warning("box_office.tmdb_failed", tmdb_id=tmdb_id, error=str(exc))
            d = {}
        revenue = _positive(d.get("revenue"))
        budget = _positive(d.get("budget"))
        if revenue:
            rows.append(_row(tmdb_id, source="tmdb", market="WW", metric="revenue_usd",
                             value=revenue, period_end=today, budget_usd=budget, now=now))

        imdb_id = m.get("imdb_id")
        if omdb_open and imdb_id and omdb_calls < settings.omdb_max_per_run:
            try:
                gross = omdb.domestic_gross(str(imdb_id))  # type: ignore[union-attr]
                omdb_calls += 1
            except OMDbAuthError as exc:
                log.warning("box_office.omdb_stopped", error=str(exc), calls=omdb_calls)
                omdb_open = False
                gross = None
            except Exception as exc:  # noqa: BLE001
                log.warning("box_office.omdb_failed", imdb_id=imdb_id, error=str(exc))
                gross = None
            if gross:
                rows.append(_row(tmdb_id, source="omdb", market="US", metric="domestic_gross_usd",
                                 value=gross, period_end=today, now=now))

    if rows:
        bq.merge_rows(
            table="box_office_observations", rows=rows, merge_keys=MERGE_KEYS,
            update_fields=[c for c in _FIELDS if c not in MERGE_KEYS], insert_fields=_FIELDS,
        )
    log.info("box_office.done", movies=len(movies), observations=len(rows), omdb_calls=omdb_calls)
    return len(movies), len(rows)


def _positive(v: Any) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _row(tmdb_id: int, *, source: str, market: str, metric: str, value: float,
         period_end: date, now: datetime, budget_usd: int | None = None) -> BoxOfficeObservationRow:
    return BoxOfficeObservationRow(
        tmdb_id=tmdb_id, source=source, market=market, metric=metric,  # type: ignore[arg-type]
        value=float(value), cumulative=True, period_start=None, period_end=period_end,
        budget_usd=budget_usd, observed_at=now,
    )


def _select_candidates(
    bq: BigQueryClient, settings: Settings, today: date, *, cap: int
) -> list[dict[str, Any]]:
    """Tracked movies released 1..max_age days ago, not observed in the last refresh window."""
    sql = f"""
    SELECT m.tmdb_id, m.imdb_id, m.title, m.primary_release_date, m.popularity
    FROM `{bq.project}.{bq.dataset}.movies` m
    LEFT JOIN (
      SELECT tmdb_id, MAX(observed_at) AS last_observed
      FROM `{bq.project}.{bq.dataset}.box_office_observations`
      WHERE source IN ('tmdb', 'omdb')
      GROUP BY tmdb_id
    ) o ON o.tmdb_id = m.tmdb_id
    WHERE m.primary_release_date BETWEEN DATE_SUB(@today, INTERVAL @max_age DAY)
                                    AND DATE_SUB(@today, INTERVAL 1 DAY)
      AND EXISTS (
        SELECT 1 FROM `{bq.project}.{bq.dataset}.trailers` t
        WHERE t.content_kind = 'movie' AND t.movie_tmdb_id = m.tmdb_id
      )
      AND (o.last_observed IS NULL
           OR o.last_observed < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @refresh DAY))
    ORDER BY m.popularity DESC
    LIMIT @cap
    """
    return bq.query(sql, {
        "today": today, "max_age": settings.box_office_max_age_days,
        "refresh": settings.box_office_refresh_days, "cap": cap,
    })
