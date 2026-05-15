"""BigQuery reads that power the weekly/monthly digest email.

Three sections, three queries:
  1. New trailers added since the cutoff.
  2. Top-N currently-tracked trailers by view delta over the period.
  3. Database stats grouped by origin country.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from movie_trailers.clients.bigquery import BigQueryClient


def _table(bq: BigQueryClient, name: str) -> str:
    return f"`{bq.project}.{bq.dataset}.{name}`"


def fetch_new_trailers(
    bq: BigQueryClient, *, cutoff_date: date
) -> list[dict[str, Any]]:
    """Trailers whose `first_seen_at` is on/after `cutoff_date`, with their latest stats.

    Latest stats come from the most recent row in `trailer_stats_daily` for each
    video. The partition filter on `trailer_stats_daily` is satisfied by the same
    cutoff (a trailer first seen on day X cannot have stats older than X).
    `release_date` is the movie's `primary_release_date` for movies, or the
    season's `air_date` for TV. Ordering is by trailer `published_at DESC`.
    """
    sql = f"""
    WITH latest_stats AS (
      SELECT
        youtube_video_id,
        ARRAY_AGG(
          STRUCT(view_count, like_count, comment_count, collected_date)
          ORDER BY collected_date DESC LIMIT 1
        )[OFFSET(0)] AS s
      FROM {_table(bq, "trailer_stats_daily")}
      WHERE collected_date >= @cutoff_date
      GROUP BY youtube_video_id
    )
    SELECT
      t.youtube_video_id,
      t.name                                            AS trailer_name,
      t.video_type,
      t.content_kind,
      t.first_seen_at,
      t.published_at                                    AS trailer_published_at,
      COALESCE(m.primary_release_date, ts.air_date)     AS release_date,
      t.tracking_status,
      COALESCE(m.title, tv.name)                        AS title,
      COALESCE(m.poster_path, tv.poster_path)           AS poster_path,
      ls.s.view_count                                   AS view_count,
      ls.s.like_count                                   AS like_count,
      ls.s.comment_count                                AS comment_count
    FROM {_table(bq, "trailers")} t
    LEFT JOIN {_table(bq, "movies")}     m  ON t.movie_tmdb_id = m.tmdb_id
    LEFT JOIN {_table(bq, "tv_shows")}   tv ON t.tv_tmdb_id    = tv.tmdb_id
    LEFT JOIN {_table(bq, "tv_seasons")} ts ON t.tv_tmdb_id    = ts.tv_tmdb_id
                                            AND t.tv_season_number = ts.season_number
    LEFT JOIN latest_stats ls USING (youtube_video_id)
    WHERE DATE(t.first_seen_at) >= @cutoff_date
    ORDER BY t.published_at DESC NULLS LAST
    """
    return bq.query(sql, params={"cutoff_date": cutoff_date})


def fetch_top_tracked_trailers(
    bq: BigQueryClient, *, cutoff_date: date, limit: int
) -> list[dict[str, Any]]:
    """Most-recently-published `limit` active trailers, with view/like deltas.

    Delta = latest view_count - earliest view_count within the window. Trailers
    discovered mid-window get their full curve since first capture. Ordering is
    by trailer `published_at DESC`; the limit is applied after sort.
    """
    sql = f"""
    WITH window_stats AS (
      SELECT
        youtube_video_id,
        MAX_BY(view_count, collected_date)    AS view_count_now,
        MIN_BY(view_count, collected_date)    AS view_count_then,
        MAX_BY(like_count, collected_date)    AS like_count_now,
        MIN_BY(like_count, collected_date)    AS like_count_then
      FROM {_table(bq, "trailer_stats_daily")}
      WHERE collected_date >= @cutoff_date
      GROUP BY youtube_video_id
    )
    SELECT
      t.youtube_video_id,
      t.name                                                       AS trailer_name,
      t.video_type,
      t.content_kind,
      t.published_at                                               AS trailer_published_at,
      t.tracking_end_date,
      COALESCE(m.primary_release_date, ts.air_date)                AS release_date,
      COALESCE(m.title, tv.name)                                   AS title,
      COALESCE(m.poster_path, tv.poster_path)                      AS poster_path,
      ws.view_count_now                                            AS view_count,
      (ws.view_count_now - ws.view_count_then)                     AS delta_views,
      ws.like_count_now                                            AS like_count,
      (ws.like_count_now - ws.like_count_then)                     AS delta_likes
    FROM {_table(bq, "trailers")} t
    JOIN window_stats ws USING (youtube_video_id)
    LEFT JOIN {_table(bq, "movies")}     m  ON t.movie_tmdb_id = m.tmdb_id
    LEFT JOIN {_table(bq, "tv_shows")}   tv ON t.tv_tmdb_id    = tv.tmdb_id
    LEFT JOIN {_table(bq, "tv_seasons")} ts ON t.tv_tmdb_id    = ts.tv_tmdb_id
                                            AND t.tv_season_number = ts.season_number
    WHERE t.tracking_status = 'active'
    ORDER BY t.published_at DESC NULLS LAST
    LIMIT @top_n
    """
    return bq.query(sql, params={"cutoff_date": cutoff_date, "top_n": limit})


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
    """
    today = today or date.today()
    if period == "week":
        return today - timedelta(days=7)
    if period == "month":
        return today - timedelta(days=30)
    raise ValueError(f"unknown digest period: {period!r} (expected 'week' or 'month')")
