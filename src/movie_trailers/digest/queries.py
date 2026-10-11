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
        -- usable transcript: a text row whose quality gate passed (rows written
        -- before the gate existed carry NULL quality and count as usable)
        (x.text IS NOT NULL AND COALESCE(x.quality, 'ok') IN ('ok', 'suspect')) AS has_transcript,
        t.comments_at_discovery_captured_at,
        t.comments_pre_release_captured_at,
        country
      FROM {_table(bq, "trailers")} t
      LEFT JOIN {_table(bq, "movies")}   m  ON t.movie_tmdb_id = m.tmdb_id
      LEFT JOIN {_table(bq, "tv_shows")} tv ON t.tv_tmdb_id    = tv.tmdb_id
      LEFT JOIN {_table(bq, "trailer_transcripts")} x ON x.youtube_video_id = t.youtube_video_id
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
      COUNTIF(has_transcript)                                                           AS with_transcript,
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


def fetch_signal_vs_outcome(bq: BigQueryClient, *, days: int = 56, limit: int = 40) -> list[dict[str, Any]]:
    """Movies released in the last `days` days that have any box-office observation,
    next to their pre-release trailer signal and the opening_tier call (if one was made).

    Read-only. Returns [] when the box-office table isn't provisioned yet.
    """
    if not bq.table_exists("vw_movie_box_office"):
        return []
    has_news = bq.table_exists("movie_news_daily")
    news_cte = f"""
    news AS (
      SELECT n.tmdb_id, SUM(n.articles_used) AS news_pre_release, AVG(n.avg_tone) AS news_tone
      FROM {_table(bq, "movie_news_daily")} n
      JOIN rel r ON r.tmdb_id = n.tmdb_id
      WHERE n.date >= DATE_SUB(CURRENT_DATE(), INTERVAL @days + 14 DAY)
        AND n.date BETWEEN DATE_SUB(r.rel, INTERVAL 14 DAY) AND DATE_SUB(r.rel, INTERVAL 1 DAY)
      GROUP BY n.tmdb_id
    ),""" if has_news else """
    news AS (SELECT CAST(NULL AS INT64) AS tmdb_id, CAST(NULL AS INT64) AS news_pre_release,
                    CAST(NULL AS FLOAT64) AS news_tone),"""
    sql = f"""
    WITH rel AS (
      SELECT tmdb_id, title, primary_release_date AS rel, origin_countries
      FROM {_table(bq, "movies")}
      WHERE primary_release_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
                                     AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
    ),
    per_trailer AS (
      SELECT t.movie_tmdb_id, s.youtube_video_id,
             ARRAY_AGG(s.view_count ORDER BY s.collected_date DESC LIMIT 1)[OFFSET(0)] AS views
      FROM {_table(bq, "trailer_stats_daily")} s
      JOIN {_table(bq, "trailers")} t USING (youtube_video_id)
      JOIN rel r ON r.tmdb_id = t.movie_tmdb_id
      WHERE t.content_kind = 'movie'
        AND s.collected_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 500 DAY)
        AND s.collected_date <= r.rel
      GROUP BY 1, 2
    ),
    sig AS (SELECT movie_tmdb_id, SUM(views) AS total_views FROM per_trailer GROUP BY 1),
    peak AS (
      SELECT t.movie_tmdb_id, MAX(v.delta_views) AS peak_daily_views
      FROM {_table(bq, "vw_trailer_daily_metrics")} v
      JOIN {_table(bq, "trailers")} t USING (youtube_video_id)
      JOIN rel r ON r.tmdb_id = t.movie_tmdb_id
      WHERE v.collected_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 500 DAY)
      GROUP BY 1
    ),
    {news_cte}
    bo AS (
      SELECT tmdb_id,
             MAX(IF(market = 'BR' AND metric = 'admissions', value, NULL)) AS br_admissions,
             MAX(IF(market = 'IN' AND metric = 'india_net_inr', value, NULL)) AS in_net_inr,
             MAX(IF(market = 'US', value, NULL)) AS us_gross_usd,
             MAX(IF(market = 'WW' AND metric = 'revenue_usd', value, NULL)) AS ww_revenue_usd,
             MAX(IF(market = 'WW' AND metric IN ('gross_inr', 'worldwide_gross_inr'), value, NULL)) AS ww_gross_inr,
             MAX(through_date) AS through_date
      FROM {_table(bq, "vw_movie_box_office")}
      GROUP BY tmdb_id
    ),
    pred AS (
      SELECT movie_tmdb_id, ANY_VALUE(predicted_value) AS predicted_tier,
             ANY_VALUE(status) AS status, ANY_VALUE(observed_market) AS observed_market
      FROM {_table(bq, "content_predictions")}
      WHERE kind = 'opening_tier'
      GROUP BY movie_tmdb_id
    )
    SELECT r.tmdb_id, r.title, r.rel AS release_date,
           ARRAY_TO_STRING(r.origin_countries, ',') AS countries,
           sig.total_views, peak.peak_daily_views, news.news_pre_release, news.news_tone,
           bo.br_admissions, bo.in_net_inr, bo.us_gross_usd, bo.ww_revenue_usd, bo.ww_gross_inr,
           bo.through_date,
           pred.predicted_tier, pred.status AS prediction_status, pred.observed_market
    FROM rel r
    JOIN bo ON bo.tmdb_id = r.tmdb_id
    LEFT JOIN sig ON sig.movie_tmdb_id = r.tmdb_id
    LEFT JOIN peak ON peak.movie_tmdb_id = r.tmdb_id
    LEFT JOIN pred ON pred.movie_tmdb_id = r.tmdb_id
    LEFT JOIN news ON news.tmdb_id = r.tmdb_id
    ORDER BY COALESCE(bo.br_admissions, bo.in_net_inr, bo.us_gross_usd, bo.ww_revenue_usd, bo.ww_gross_inr) DESC
    LIMIT @limit
    """
    return bq.query(sql, {"days": days, "limit": limit})
