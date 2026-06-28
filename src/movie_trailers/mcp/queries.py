"""Read-only BigQuery queries backing the MCP server.

Conventions that match the rest of the codebase:
- Derived signals (Δviews/Δlikes/ratios) come from `vw_trailer_daily_metrics`,
  never the raw `trailer_stats_daily` table.
- The partitioned tables (`trailer_stats_daily`, `trailer_comments_snapshots`)
  declare `require_partition_filter = TRUE`, so every query here includes a
  `*_date` predicate — even a wide one — or BigQuery rejects it.
- Image paths are stored raw; `_image_url` turns them into full URLs at this
  layer so MCP consumers never need to know the TMDB CDN convention.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from movie_trailers.clients.bigquery import BigQueryClient

TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"


def _table(bq: BigQueryClient, name: str) -> str:
    return f"`{bq.project}.{bq.dataset}.{name}`"


def _image_url(path: str | None, size: str = "w500") -> str | None:
    """Build a full TMDB image URL from a stored relative path (e.g. '/abc.jpg')."""
    if not path:
        return None
    return f"{TMDB_IMAGE_BASE}/{size}{path}"


# A single CTE that flattens the polymorphic `trailers` table down to a uniform
# shape (title + poster + origin countries) regardless of movie vs. tv. Reused
# by search_trailers and trending so both speak the same vocabulary.
_TRAILER_TITLES_CTE = """
trailer_titles AS (
  SELECT
    t.youtube_video_id,
    t.content_kind,
    t.movie_tmdb_id,
    t.tv_tmdb_id,
    t.tv_season_number,
    t.name                       AS trailer_name,
    t.video_type,
    t.tracking_status,
    t.published_at,
    t.tracking_end_date,
    t.thumbnail_url,
    COALESCE(m.title, tv.name)                       AS title,
    COALESCE(m.primary_release_date, se.air_date)    AS release_date,
    COALESCE(m.poster_path, tv.poster_path)          AS poster_path,
    COALESCE(m.origin_countries, tv.origin_countries) AS origin_countries
  FROM {trailers} t
  LEFT JOIN {movies}   m  ON t.movie_tmdb_id = m.tmdb_id
  LEFT JOIN {tv_shows} tv ON t.tv_tmdb_id    = tv.tmdb_id
  LEFT JOIN {tv_seasons} se
         ON t.tv_tmdb_id = se.tv_tmdb_id AND t.tv_season_number = se.season_number
)
"""


def _titles_cte(bq: BigQueryClient) -> str:
    return _TRAILER_TITLES_CTE.format(
        trailers=_table(bq, "trailers"),
        movies=_table(bq, "movies"),
        tv_shows=_table(bq, "tv_shows"),
        tv_seasons=_table(bq, "tv_seasons"),
    )


def _decorate(row: dict[str, Any]) -> dict[str, Any]:
    """Attach a full poster_url next to the raw poster_path, in place."""
    if "poster_path" in row:
        row["poster_url"] = _image_url(row.get("poster_path"))
    return row


def search_trailers(
    bq: BigQueryClient,
    *,
    content_kind: str | None = None,
    country: str | None = None,
    tracking_status: str = "active",
    query_text: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Find tracked trailers by kind, origin country, status, and title text.

    `country` is an ISO 3166-1 code matched against the title's origin_countries.
    `query_text` is a case-insensitive substring match on movie/show title.
    Ordered by most-recently-published first.
    """
    filters = ["1 = 1"]
    params: dict[str, Any] = {"limit": min(limit, 100)}
    if content_kind:
        filters.append("content_kind = @content_kind")
        params["content_kind"] = content_kind
    if tracking_status:
        filters.append("tracking_status = @tracking_status")
        params["tracking_status"] = tracking_status
    if country:
        filters.append("@country IN UNNEST(origin_countries)")
        params["country"] = country
    if query_text:
        filters.append("LOWER(title) LIKE CONCAT('%', LOWER(@query_text), '%')")
        params["query_text"] = query_text

    sql = f"""
    WITH {_titles_cte(bq)}
    SELECT
      youtube_video_id, content_kind, movie_tmdb_id, tv_tmdb_id, tv_season_number,
      title, trailer_name, video_type,
      tracking_status, published_at, release_date, tracking_end_date,
      origin_countries, poster_path, thumbnail_url
    FROM trailer_titles
    WHERE {' AND '.join(filters)}
    ORDER BY published_at DESC
    LIMIT @limit
    """
    return [_decorate(r) for r in bq.query(sql, params)]


def trailer_metrics(
    bq: BigQueryClient,
    *,
    youtube_video_id: str,
    days: int = 30,
) -> list[dict[str, Any]]:
    """Per-day metrics (Δviews/Δlikes/ratios) for one trailer over the last `days`.

    Reads the derived view, not the raw stats table. The explicit collected_date
    floor both bounds the window and lets BigQuery prune partitions.
    """
    sql = f"""
    SELECT
      collected_date, view_count, like_count, comment_count,
      delta_views, delta_likes, delta_comments,
      like_view_ratio, comment_view_ratio, days_since_publish
    FROM {_table(bq, "vw_trailer_daily_metrics")}
    WHERE youtube_video_id = @youtube_video_id
      AND collected_date >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
    ORDER BY collected_date
    """
    return bq.query(sql, {"youtube_video_id": youtube_video_id, "days": days})


def top_comments(
    bq: BigQueryClient,
    *,
    youtube_video_id: str,
    snapshot_kind: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Top captured comments for a trailer, ordered by YouTube's relevance rank.

    `snapshot_kind` selects 'at_discovery' or 'pre_release'; omit to take the
    most recent snapshot of either kind. The wide snapshot_date floor satisfies
    the table's require_partition_filter.
    """
    filters = [
        "youtube_video_id = @youtube_video_id",
        "snapshot_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 1000 DAY)",
    ]
    params: dict[str, Any] = {
        "youtube_video_id": youtube_video_id,
        "limit": min(limit, 30),
    }
    if snapshot_kind:
        filters.append("snapshot_kind = @snapshot_kind")
        params["snapshot_kind"] = snapshot_kind
    else:
        # No kind requested: restrict to the latest snapshot_date we hold so the
        # two lifetime captures don't interleave by rank.
        filters.append(
            f"snapshot_date = (SELECT MAX(snapshot_date) FROM {_table(bq, 'trailer_comments_snapshots')} "
            "WHERE youtube_video_id = @youtube_video_id "
            "AND snapshot_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 1000 DAY))"
        )

    sql = f"""
    SELECT
      snapshot_kind, snapshot_date, rank, text, like_count,
      total_reply_count, author_display_name, published_at
    FROM {_table(bq, "trailer_comments_snapshots")}
    WHERE {' AND '.join(filters)}
    ORDER BY rank
    LIMIT @limit
    """
    return bq.query(sql, params)


def trending(
    bq: BigQueryClient,
    *,
    period_days: int = 7,
    content_kind: str | None = None,
    country: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Trailers with the largest total view growth over the last `period_days`.

    Sums delta_views from the metrics view across the window, then joins back to
    titles. Optional content_kind / origin-country filters.
    """
    filters = ["1 = 1"]
    params: dict[str, Any] = {"period_days": period_days, "limit": min(limit, 100)}
    if content_kind:
        filters.append("tt.content_kind = @content_kind")
        params["content_kind"] = content_kind
    if country:
        filters.append("@country IN UNNEST(tt.origin_countries)")
        params["country"] = country

    sql = f"""
    WITH {_titles_cte(bq)},
    growth AS (
      SELECT
        youtube_video_id,
        SUM(delta_views)  AS views_gained,
        SUM(delta_likes)  AS likes_gained,
        MAX(view_count)   AS view_count,
        AVG(like_view_ratio) AS avg_like_view_ratio
      FROM {_table(bq, "vw_trailer_daily_metrics")}
      WHERE collected_date >= DATE_SUB(CURRENT_DATE(), INTERVAL @period_days DAY)
      GROUP BY youtube_video_id
    )
    SELECT
      tt.youtube_video_id, tt.content_kind, tt.title, tt.trailer_name,
      tt.tracking_status, tt.release_date, tt.origin_countries, tt.poster_path,
      g.views_gained, g.likes_gained, g.view_count, g.avg_like_view_ratio
    FROM growth g
    JOIN trailer_titles tt USING (youtube_video_id)
    WHERE {' AND '.join(filters)} AND g.views_gained IS NOT NULL
    ORDER BY g.views_gained DESC
    LIMIT @limit
    """
    return [_decorate(r) for r in bq.query(sql, params)]


# --- Engagement-quality axis (like/view ratio) ---
# Fraction change over the window that separates a verdict from "steady". A
# movie's like/view ratio drifting <5% across the window reads as flat noise;
# beyond that we call it gaining/losing. Tuned against the hypothesis check:
# 77% of new-trailer launches followed a measurable decline in the prior trailer.
_STEADY_BAND = 0.05
_MIN_POINTS = 5  # fewer days than this can't support a trend

# --- Demand axis (view velocity = daily new views) ---
# velocity_vs_peak = recent daily new-views ÷ the window's peak. These cut points
# split a movie's demand into holding / cooling / spent. The measurement that
# motivated this axis: the median trailer's view-velocity half-life is ~6 days,
# and it carries information independent of like/view (corr ≈ +0.10).
_HOLDING_VS_PEAK = 0.50
_COOLING_VS_PEAK = 0.20
_REBOUND_RATIO = 1.25  # recent vs prior-week velocity ratio that flags a rebound
_RECENT_DAYS = 7  # trailing window for "current" velocity


def _linfit(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """Ordinary least-squares slope + intercept; None if x has no spread."""
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys, strict=True))
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


def _velocity_axis(series: list[dict[str, Any]]) -> dict[str, Any]:
    """Demand-momentum metrics from the movie's daily new-views (delta_views).

    delta_views is summed per-trailer upstream, so a new trailer's back-catalog
    views never create a one-day step. Returns current vs peak velocity, a
    post-peak half-life (days for new-views to halve once falling), and a
    demand_state: rebounding / holding / cooling / spent.
    """
    pts = [
        (r["collected_date"], float(r["delta_views"]))
        for r in series
        if r.get("delta_views") is not None
    ]
    if len(pts) < _MIN_POINTS:
        return {"demand_state": "insufficient_data"}

    last_day = pts[-1][0]
    vals = [v for _, v in pts]
    peak_velocity = max(vals)
    peak_idx = max(range(len(pts)), key=lambda i: pts[i][1])

    def _window_mean(lo: int, hi: int) -> float | None:
        sel = [v for d, v in pts if lo < (last_day - d).days <= hi]
        return sum(sel) / len(sel) if sel else None

    current_velocity = _window_mean(-1, _RECENT_DAYS - 1)  # last 7 days incl. today
    prior_velocity = _window_mean(_RECENT_DAYS - 1, 2 * _RECENT_DAYS - 1)
    vs_peak = (
        current_velocity / peak_velocity
        if current_velocity is not None and peak_velocity > 0
        else None
    )
    rebound = (
        current_velocity is not None
        and prior_velocity is not None
        and prior_velocity > 0
        and current_velocity > _REBOUND_RATIO * prior_velocity
    )

    # Post-peak decay half-life: fit ln(new-views) vs day on the falling tail.
    half_life: float | None = None
    tail = [(i, v) for i, (_, v) in enumerate(pts) if i > peak_idx and v > 0]
    if len(tail) >= 4:
        fit = _linfit([float(i) for i, _ in tail], [math.log(v) for _, v in tail])
        if fit and fit[0] < 0:
            half_life = math.log(2) / -fit[0]

    if rebound:
        demand_state = "rebounding"
    elif vs_peak is None:
        demand_state = "unknown"
    elif vs_peak >= _HOLDING_VS_PEAK:
        demand_state = "holding"
    elif vs_peak >= _COOLING_VS_PEAK:
        demand_state = "cooling"
    else:
        demand_state = "spent"

    return {
        "demand_state": demand_state,
        "current_velocity": current_velocity,
        "peak_velocity": peak_velocity,
        "velocity_vs_peak": vs_peak,
        "post_peak_half_life_days": half_life,
    }


def movie_engagement_trend(
    bq: BigQueryClient,
    *,
    movie_tmdb_id: int,
    days: int = 30,
) -> dict[str, Any]:
    """Score a movie on two independent engagement axes over time.

    Both are measured at the *movie* level, aggregated across all its trailers:

    - **Quality** — like/view ratio, classified gaining/losing/steady from a
      least-squares fit (±5% start→end band).
    - **Demand** — view velocity (daily new views), classified rebounding/
      holding/cooling/spent from how far current velocity has fallen below the
      window's peak, plus a post-peak half-life.

    The two axes are near-uncorrelated (≈+0.10 in this dataset), so a movie can
    be "loved but fading" (steady quality, spent demand) or the reverse. New
    trailers in the window are surfaced because that's the lever studios pull
    when demand sags (validated: 77% of in-window launches followed a decline,
    and a launch lifts movie view velocity ~2.8× while barely moving like/view).
    """
    series = bq.query(
        f"""
        SELECT
          v.collected_date,
          SUM(v.like_count)   AS likes,
          SUM(v.view_count)   AS views,
          SUM(v.delta_views)  AS delta_views,
          SAFE_DIVIDE(SUM(v.like_count), SUM(v.view_count)) AS like_view_ratio
        FROM {_table(bq, "vw_trailer_daily_metrics")} v
        JOIN {_table(bq, "trailers")} t USING (youtube_video_id)
        WHERE t.content_kind = 'movie'
          AND t.movie_tmdb_id = @movie_tmdb_id
          AND v.collected_date >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        GROUP BY v.collected_date
        HAVING views > 0
        ORDER BY v.collected_date
        """,
        {"movie_tmdb_id": movie_tmdb_id, "days": days},
    )

    trailers = bq.query(
        f"""
        SELECT youtube_video_id, name AS trailer_name, video_type,
               published_at, tracking_status
        FROM {_table(bq, "trailers")}
        WHERE content_kind = 'movie' AND movie_tmdb_id = @movie_tmdb_id
        ORDER BY published_at DESC
        """,
        {"movie_tmdb_id": movie_tmdb_id},
    )
    launches_in_window = [
        t
        for t in trailers
        if t["published_at"] is not None
        and (date.today() - t["published_at"].date()).days <= days
    ]

    result: dict[str, Any] = {
        "movie_tmdb_id": movie_tmdb_id,
        "window_days": days,
        "n_trailers": len(trailers),
        "new_trailers_in_window": len(launches_in_window),
        "recent_launches": launches_in_window,
        "data_points": len(series),
        "series": series,
    }

    # --- Demand axis (view velocity) ---
    demand = _velocity_axis(series)
    result["demand"] = demand

    # --- Quality axis (like/view ratio) ---
    if len(series) < _MIN_POINTS:
        result["quality_classification"] = "insufficient_data"
        result["verdict"] = "insufficient_data"
        return result

    x0 = series[0]["collected_date"]
    xs = [float((r["collected_date"] - x0).days) for r in series]
    ys = [float(r["like_view_ratio"]) for r in series]
    fit = _linfit(xs, ys)
    if fit is None:
        result["quality_classification"] = "insufficient_data"
        result["verdict"] = "insufficient_data"
        return result

    slope, intercept = fit
    fit_start = intercept + slope * xs[0]
    fit_end = intercept + slope * xs[-1]
    pct_change = (fit_end - fit_start) / fit_start if fit_start else 0.0

    if pct_change > _STEADY_BAND:
        quality = "gaining"
    elif pct_change < -_STEADY_BAND:
        quality = "losing"
    else:
        quality = "steady"

    result.update(
        quality_classification=quality,
        slope_per_day=slope,
        pct_change_over_window=pct_change,
        ratio_start=fit_start,
        ratio_end=fit_end,
        avg_like_view_ratio=sum(ys) / len(ys),
        verdict=f"{quality} quality / {demand['demand_state']} demand",
    )
    return result


def _summarize_units(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up per-unit like/view changes: counts, shares, median, view-weighted."""
    n = len(items)
    if n == 0:
        return {"units": 0}
    pcts = sorted(m["pct"] for m in items)
    median = pcts[n // 2] if n % 2 else (pcts[n // 2 - 1] + pcts[n // 2]) / 2
    losing = sum(1 for p in pcts if p < -_STEADY_BAND)
    gaining = sum(1 for p in pcts if p > _STEADY_BAND)
    weight = sum(m["w"] for m in items)
    vw = sum(m["pct"] * m["w"] for m in items) / weight if weight else None
    return {
        "units": n,
        "losing": losing,
        "steady": n - losing - gaining,
        "gaining": gaining,
        "losing_share": losing / n,
        "median_pct_change": median,
        "view_weighted_pct_change": vw,
    }


def engagement_decay(
    bq: BigQueryClient,
    *,
    days: int = 45,
    unit: str = "movie",
) -> dict[str, Any]:
    """How like/view engagement trends over the window, per movie or per trailer.

    `unit="movie"` (default): fit each movie's summed-trailer like/view ratio.
    `unit="trailer"`: fit each individual trailer's like/view ratio.

    Either way, the per-unit start→end changes are summarized across units
    (losing/steady/gaining counts, median and view-weighted change) and split by
    the parent movie's trailer count — so you can see whether single-trailer
    movies decay harder than ones that keep launching. Units need ≥10 days of
    history to be fit.
    """
    if unit not in ("movie", "trailer"):
        raise ValueError("unit must be 'movie' or 'trailer'")
    # The grouping key is the unit; movie mode sums a movie's trailers per day,
    # trailer mode keeps each trailer separate. Both carry the parent movie id
    # so results can be bucketed by the movie's trailer count.
    key = "t.movie_tmdb_id" if unit == "movie" else "v.youtube_video_id"
    rows = bq.query(
        f"""
        WITH base AS (
          SELECT {key} AS unit_id,
                 ANY_VALUE(t.movie_tmdb_id) AS movie_tmdb_id,
                 v.collected_date,
                 SAFE_DIVIDE(SUM(v.like_count), SUM(v.view_count)) AS lv,
                 SUM(v.view_count) AS views,
                 DATE_DIFF(v.collected_date, CURRENT_DATE(), DAY) AS x
          FROM {_table(bq, "vw_trailer_daily_metrics")} v
          JOIN {_table(bq, "trailers")} t USING (youtube_video_id)
          WHERE t.content_kind = 'movie' AND t.movie_tmdb_id IS NOT NULL
            AND v.collected_date >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
          GROUP BY unit_id, v.collected_date, x
        ),
        fit AS (
          SELECT unit_id, ANY_VALUE(movie_tmdb_id) AS movie_tmdb_id, COUNT(*) AS n,
                 SAFE_DIVIDE(COVAR_POP(x, lv), VAR_POP(x)) AS slope,
                 AVG(lv) AS mean_lv, AVG(x) AS mean_x,
                 MIN(x) AS x0, MAX(x) AS x1, MAX(views) AS peak_views
          FROM base WHERE lv > 0
          GROUP BY unit_id HAVING n >= 10 AND VAR_POP(x) > 0 AND AVG(lv) > 0
        ),
        tc AS (
          SELECT movie_tmdb_id, COUNT(*) AS n_trailers
          FROM {_table(bq, "trailers")}
          WHERE content_kind = 'movie' AND movie_tmdb_id IS NOT NULL
          GROUP BY 1
        )
        SELECT f.slope, f.mean_lv, f.mean_x, f.x0, f.x1, f.peak_views, tc.n_trailers
        FROM fit f JOIN tc USING (movie_tmdb_id)
        """,
        {"days": days},
    )
    units: list[dict[str, Any]] = []
    for r in rows:
        slope = r["slope"]
        if slope is None:
            continue
        intercept = r["mean_lv"] - slope * r["mean_x"]
        start = intercept + slope * r["x0"]
        end = intercept + slope * r["x1"]
        if not start or start <= 0:
            continue
        units.append(
            {"pct": (end - start) / start, "w": r["peak_views"] or 0, "nt": r["n_trailers"]}
        )

    def bucket(lo: int, hi: int | None) -> list[dict[str, Any]]:
        return [m for m in units if m["nt"] >= lo and (hi is None or m["nt"] <= hi)]

    return {
        "unit": unit,
        "days": days,
        **_summarize_units(units),
        "by_movie_trailer_count": {
            "1": _summarize_units(bucket(1, 1)),
            "2-3": _summarize_units(bucket(2, 3)),
            "4+": _summarize_units(bucket(4, None)),
        },
    }


# --- Comment-excitement decay (distilled local model) ------------------------
_EXCITE_STEADY_BAND = 2.0  # ±points first→last treated as "steady"


def _latest_excitement_version(bq: BigQueryClient) -> str | None:
    rows = bq.query(
        f"""
        SELECT model_version
        FROM {_table(bq, "trailer_comment_excitement")}
        WHERE scored_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 730 DAY)
        GROUP BY model_version
        ORDER BY MAX(scored_date) DESC, model_version DESC
        LIMIT 1
        """
    )
    return rows[0]["model_version"] if rows else None


def _summarize_excitement(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up per-movie first→last excitement deltas (in points)."""
    n = len(items)
    if n == 0:
        return {"movies": 0}
    deltas = sorted(m["delta"] for m in items)
    median = deltas[n // 2] if n % 2 else (deltas[n // 2 - 1] + deltas[n // 2]) / 2
    declining = sum(1 for d in deltas if d < -_EXCITE_STEADY_BAND)
    rising = sum(1 for d in deltas if d > _EXCITE_STEADY_BAND)
    return {
        "movies": n,
        "declining": declining,
        "steady": n - declining - rising,
        "rising": rising,
        "declining_share": declining / n,
        "median_first_last_delta": round(median, 2),
        "mean_slope_per_trailer": round(sum(m["slope"] for m in items) / n, 3),
    }


def comment_excitement_decay(
    bq: BigQueryClient,
    *,
    model_version: str | None = None,
) -> dict[str, Any]:
    """Does comment excitement decay across a movie's sequential trailers?

    For each movie with ≥2 scored trailers, fits a least-squares line of the
    distilled excitement score (0–100) against the trailer's ordinal position
    (by published_at) and records the first→last change. Results are summarized
    across movies and split by trailer count, so the dose-response is visible
    (in this dataset, movies with 5+ trailers fall hardest). Defaults to the
    latest model_version.
    """
    version = model_version or _latest_excitement_version(bq)
    if version is None:
        return {"model_version": None, "movies": 0, "note": "no excitement scores yet"}
    rows = bq.query(
        f"""
        WITH e AS (
          SELECT movie_tmdb_id, trailer_ordinal, excitement, n_trailers
          FROM {_table(bq, "vw_movie_trailer_excitement")}
          WHERE model_version = @mv
        ),
        agg AS (
          SELECT movie_tmdb_id,
                 ANY_VALUE(n_trailers) AS n_trailers,
                 COUNT(*) AS n,
                 SAFE_DIVIDE(COVAR_POP(trailer_ordinal, excitement), VAR_POP(trailer_ordinal)) AS slope,
                 ARRAY_AGG(excitement ORDER BY trailer_ordinal)[OFFSET(0)] AS first_exc,
                 ARRAY_AGG(excitement ORDER BY trailer_ordinal DESC)[OFFSET(0)] AS last_exc
          FROM e
          GROUP BY movie_tmdb_id
          HAVING n >= 2 AND VAR_POP(trailer_ordinal) > 0
        )
        SELECT n_trailers, slope, first_exc, last_exc FROM agg
        """,
        {"mv": version},
    )
    units = [
        {"delta": r["last_exc"] - r["first_exc"], "slope": r["slope"] or 0.0, "nt": r["n_trailers"]}
        for r in rows
    ]

    def bucket(lo: int, hi: int | None) -> list[dict[str, Any]]:
        return [m for m in units if m["nt"] >= lo and (hi is None or m["nt"] <= hi)]

    return {
        "model_version": version,
        **_summarize_excitement(units),
        "by_movie_trailer_count": {
            "2": _summarize_excitement(bucket(2, 2)),
            "3-4": _summarize_excitement(bucket(3, 4)),
            "5+": _summarize_excitement(bucket(5, None)),
        },
    }


def movie_excitement_trend(
    bq: BigQueryClient,
    *,
    movie_tmdb_id: int,
    model_version: str | None = None,
) -> dict[str, Any]:
    """One movie's comment-excitement across its trailer sequence.

    Returns the ordered trailers (ordinal, excitement), the first→last delta, the
    per-trailer slope, and a one-line verdict — the per-movie view behind
    `comment_excitement_decay`.
    """
    version = model_version or _latest_excitement_version(bq)
    if version is None:
        return {"movie_tmdb_id": movie_tmdb_id, "model_version": None, "trailers": []}
    rows = bq.query(
        f"""
        SELECT trailer_ordinal, youtube_video_id, excitement, published_at, n_trailers
        FROM {_table(bq, "vw_movie_trailer_excitement")}
        WHERE model_version = @mv AND movie_tmdb_id = @movie
        ORDER BY trailer_ordinal
        """,
        {"mv": version, "movie": movie_tmdb_id},
    )
    series = [
        {
            "trailer_ordinal": r["trailer_ordinal"],
            "youtube_video_id": r["youtube_video_id"],
            "excitement": round(r["excitement"], 2),
            "published_at": r["published_at"],
        }
        for r in rows
    ]
    out: dict[str, Any] = {
        "movie_tmdb_id": movie_tmdb_id,
        "model_version": version,
        "n_trailers": rows[0]["n_trailers"] if rows else 0,
        "trailers": series,
    }
    if len(series) >= 2:
        first, last = series[0]["excitement"], series[-1]["excitement"]
        delta = last - first
        fit = _linfit(
            [float(s["trailer_ordinal"]) for s in series],
            [float(s["excitement"]) for s in series],
        )
        out["first_last_delta"] = round(delta, 2)
        out["slope_per_trailer"] = round(fit[0], 3) if fit else None
        out["verdict"] = (
            "cooling — later trailers draw less-excited comments"
            if delta < -_EXCITE_STEADY_BAND
            else "warming — later trailers draw more-excited comments"
            if delta > _EXCITE_STEADY_BAND
            else "steady excitement across trailers"
        )
    return out
