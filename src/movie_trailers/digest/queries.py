"""BigQuery read that powers the weekly/monthly digest email.

The digest is a single aggregation: trailer counts grouped by origin country.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from movie_trailers.clients.bigquery import BigQueryClient


def _table(bq: BigQueryClient, name: str) -> str:
    return f"`{bq.project}.{bq.dataset}.{name}`"


def fetch_country_stats(bq: BigQueryClient) -> list[dict[str, Any]]:
    """Per-origin-country trailer counts.

    A trailer with multiple origin countries is counted once per country, so the
    column totals can exceed the unique trailer count — this matches the
    "trailers attributable to country X" reading the user wants.
    """
    sql = f"""
    WITH trailer_country AS (
      SELECT
        t.youtube_video_id,
        t.tracking_status,
        t.transcript_captured_at,
        t.comments_at_discovery_captured_at,
        t.comments_pre_release_captured_at,
        country
      FROM {_table(bq, "trailers")} t
      LEFT JOIN {_table(bq, "movies")}   m  ON t.movie_tmdb_id = m.tmdb_id
      LEFT JOIN {_table(bq, "tv_shows")} tv ON t.tv_tmdb_id    = tv.tmdb_id
      CROSS JOIN UNNEST(
        IF(
          ARRAY_LENGTH(COALESCE(m.origin_countries, tv.origin_countries, [])) = 0,
          ['UNKNOWN'],
          COALESCE(m.origin_countries, tv.origin_countries)
        )
      ) AS country
    )
    SELECT
      country,
      COUNT(*)                                                                          AS total_trailers,
      COUNTIF(tracking_status = 'active')                                               AS active_trailers,
      COUNTIF(tracking_status = 'ended')                                                AS ended_trailers,
      COUNTIF(tracking_status = 'unavailable')                                          AS unavailable_trailers,
      COUNTIF(transcript_captured_at IS NOT NULL)                                       AS with_transcript,
      COUNTIF(comments_at_discovery_captured_at IS NOT NULL
              OR comments_pre_release_captured_at IS NOT NULL)                          AS with_comments
    FROM trailer_country
    GROUP BY country
    ORDER BY total_trailers DESC
    """
    return bq.query(sql)


def cutoff_for_period(period: str, today: date | None = None) -> date:
    """Return the inclusive lower-bound date for the digest window.

    'week' → 7 days back; 'month' → 30 days back. Anything else → ValueError.
    The digest currently only reports an aggregate snapshot, so the cutoff is
    informational (it appears in the email header) rather than load-bearing in
    the country query — kept here so a future per-window section can reuse it.
    """
    today = today or date.today()
    if period == "week":
        return today - timedelta(days=7)
    if period == "month":
        return today - timedelta(days=30)
    raise ValueError(f"unknown digest period: {period!r} (expected 'week' or 'month')")
